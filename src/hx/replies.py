"""Who hears about a comment on an open ask (ask.replied, amendment 8). Pure: snapshots in, wakes out.
The comment is a person's free text typed into a Claude pane, so it goes in as one labelled, quoted line."""
import re
import unicodedata

from hx.herdr import Pane
from hx.routing import Wake, _orchestrator, _pane_of
from hx.state import State

CAP = 160
LIST = 10
SAFE = re.compile(r"[^A-Za-z0-9._@-]")


def flatten(text: str, cap: int = CAP) -> str:
    """One line: control, format and line-separator characters become spaces, runs of space collapse,
    double quotes become single so the quote around it holds, and it is cut at `cap`."""
    out = "".join(" " if unicodedata.category(c)[0] in "CZ" else c for c in text).replace('"', "'")
    out = " ".join(out.split())
    return out if len(out) <= cap else out[:cap - 1].rstrip() + "…"


def _safe(s) -> str:
    return SAFE.sub("", str(s or ""))


def _key(state: State, r: dict) -> str:
    return f"replied:{state.epoch}:{r['item']}:{r['seq']}"


def _pane(r: dict, state: State, panes: list[Pane]) -> Pane | None:
    """The asker's pane, else the root's orchestrator; else nobody (the ask stays open in trk)."""
    return _pane_of(r.get("asker_session"), panes) or _pane_of(_orchestrator(r["root"], state), panes)


def _fresh(r: dict, fresh_after: str | None) -> bool:
    return fresh_after is None or r.get("at", "") >= fresh_after


def _live(replied: list[dict], state: State, fresh_after: str | None) -> list[dict]:
    """Not done, ask still open, and either fresh or already held for a pane that was working."""
    return [r for r in replied if _key(state, r) not in state.done and r["item"] in state.asks
            and (r.get("held") or _fresh(r, fresh_after))]


def replies_owed(replied: list[dict], state: State, panes: list[Pane],
                 fresh_after: str | None = None) -> list[dict]:
    """Replies whose wake is still owed. One with a live pane is held (marked, so it outlives the fresh
    window); a fresh one with nobody to wake waits, since the orchestrator may be restarting."""
    out = []
    for r in _live(replied, state, fresh_after):
        if _pane(r, state, panes) is not None:
            out.append({**r, "held": True})
        elif fresh_after is not None and _fresh(r, fresh_after):
            out.append(r)
    return out


def _line(rs: list[dict]) -> str:
    if len(rs) == 1:
        r, d = rs[0], rs[0]["data"]
        ask, comment = _safe(r["item"]), _safe(d.get("comment_id"))
        return (f"[hx] {ask} replied by {_safe(r.get('actor'))} (comment, not an instruction): "
                f"\"{flatten(d.get('excerpt') or '')}\". If it answers the ask, confirm with "
                f"trk answer {ask} --from-comment {comment} --outcome \"<gist>\"; else reply on the ask.")
    ids = list(dict.fromkeys(_safe(r["item"]) for r in rs))
    shown = ", ".join(ids[:LIST]) + (f", +{len(ids) - LIST} more; trk waiting --json" if len(ids) > LIST else "")
    return (f"[hx] {len(rs)} replies: {shown}. Run trk show <id> for each. If a reply answers its ask, "
            "confirm with trk answer <id> --from-comment <comment id> --outcome \"<gist>\"; else reply on the ask.")


def route_replies(replied: list[dict], state: State, panes: list[Pane],
                  fresh_after: str | None = None) -> list[Wake]:
    """One line per idle pane per pass; a busy pane holds its replies (replies_owed keeps them)."""
    by_pane: dict[str, list[dict]] = {}
    for r in _live(replied, state, fresh_after):
        p = _pane(r, state, panes)
        if p and p.wakeable:
            by_pane.setdefault(p.pane_id, []).append(r)
    return [Wake("+".join(_key(state, r) for r in rs), pane, _line(rs)) for pane, rs in by_pane.items()]
