"""Cursor rules (ADR 0002 point 2) and what the feed tells us about asks. Pure: no I/O."""
from dataclasses import dataclass, field

from hx.state import AskInfo, State


class SchemaChanged(Exception):
    pass


@dataclass
class FeedResult:
    rebuilt: bool = False
    closed: list[dict] = field(default_factory=list)
    touched: set[str] = field(default_factory=set)     # every item a row names
    replied: list[dict] = field(default_factory=list)  # live ask.replied rows on asks still open
    closes: dict[str, int] = field(default_factory=dict)  # ask -> seq of its last answer or withdrawal
    comments: list[dict] = field(default_factory=list)  # live comment.routed rows
    acked: set[str] = field(default_factory=set)        # comment ids acknowledged, history included


def is_quiet(row: dict) -> bool:
    return (row.get("data") or {}).get("via") == "migration"


def advance(state: State, reply: dict, replay: bool | None = None,
            history_before: str | None = None, fresh_after: str | None = None) -> FeedResult:
    """`fresh_after`: an ask.replied row written before it is history (hx was down or behind)."""
    if reply["schema"] != 1:
        raise SchemaChanged(f"feed schema {reply['schema']}")
    if state.epoch is None:
        state.epoch = reply["epoch"]
    elif reply["epoch"] != state.epoch or reply["head"] < state.seq:
        state.epoch, state.seq, state.asks, state.askers = reply["epoch"], 0, {}, {}
        return FeedResult(rebuilt=True)
    result = FeedResult()
    if replay is None:               # history: record asks, but wake and clear nobody
        replay = state.seq == 0
    for ask, info in state.asks.items():     # asks opened before askers existed
        state.askers.setdefault(ask, info.session)
    for row in reply["changes"]:
        kind, ask, data = row["change"], row["item"], row.get("data") or {}
        if ask:
            result.touched.add(ask)
        if kind in ("ask.opened", "ask.reopened"):
            if kind == "ask.opened":
                state.askers[ask] = row.get("session")             # None too: opened by no session
            session = state.askers.setdefault(ask, row.get("session"))   # unknown opener: first reopener
            state.asks[ask] = AskInfo(session, row["seq"], row.get("title") or "",
                                      data.get("question", ""))
        elif kind == "ask.readdressed" and ask in state.asks:
            state.asks[ask].gen = row["seq"]
        elif kind in ("ask.answered", "ask.withdrawn"):
            result.closes[ask] = row["seq"]
            history = replay or (history_before is not None and row.get("at", "") <= history_before)
            if not is_quiet(row) and not history:
                info = state.asks.get(ask)
                result.closed.append({**row, "asker_session": info.session if info else None})
            state.asks.pop(ask, None)
        elif kind == "ask.replied" and ask in state.asks:
            history = replay or (history_before is not None and row.get("at", "") <= history_before) \
                or (fresh_after is not None and row.get("at", "") < fresh_after)
            if not is_quiet(row) and not history:
                result.replied.append({**row, "asker_session": state.asks[ask].session})
        elif kind == "comment.routed":
            history = replay or (history_before is not None and row.get("at", "") <= history_before) \
                or (fresh_after is not None and row.get("at", "") < fresh_after)
            if not is_quiet(row) and not history and data.get("comment_id"):
                result.comments.append(row)
        elif kind == "comment.acknowledged" and data.get("comment_id"):
            result.acked.add(data["comment_id"])
    state.seq = reply["next"]
    return result
