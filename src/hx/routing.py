"""Who hears about an ask (spec Section 1, ADR 0002 point 10). Pure: snapshots in, actions out."""
from dataclasses import dataclass

from hx.herdr import Pane
from hx.state import State


@dataclass(frozen=True)
class Notify:
    key: str
    person: str
    channel: str
    ask: str
    text: str
    pane: str | None


@dataclass(frozen=True)
class Wake:
    key: str
    pane: str
    text: str
    needs_you: str | None = None   # the answered ask: clear the pane's needs-you item if it names it


@dataclass(frozen=True)
class Clear:
    key: str
    ask: str


def _pane_of(session: str | None, panes: list[Pane]) -> Pane | None:
    return next((p for p in panes if session and p.session == session), None)


def _orchestrator(root: str, state: State) -> str | None:
    return (state.bindings.get(root) or {}).get("orchestrator")


def _human(a: dict, person: str, state: State, panes, humans) -> list[Notify]:
    info = state.asks.get(a["id"])
    gen = info.gen if info else 0
    text = f"{a['id']} {a['title']}: {info.question if info else ''}".strip()
    asker = _pane_of(info.session if info else None, panes) or \
        _pane_of(_orchestrator(a["root"], state), panes)
    return [Notify(f"deliver:{a['id']}:{gen}:{ch}", person, ch, a["id"], text,
                   asker.pane_id if (ch == "queue" and asker) else None)
            for ch in humans.get(person, ["console"])]


def _closer_pane(row: dict, state: State, panes) -> Pane | None:
    """The first live pane of: the asker, the parent's live claimant, the orchestrator."""
    data = row["data"]
    live_parent = data.get("parent_claim_session") if data.get("parent_claim_live") else None
    for s in (row.get("asker_session"), live_parent, _orchestrator(row["root"], state)):
        if p := _pane_of(s, panes):
            return p
    return None


def still_pending(closed: list[dict], state: State, panes) -> list[dict]:
    """Closed rows whose wake is still owed: not done, not quiet, and someone live to wake."""
    return [r for r in closed
            if r["data"].get("reason") != "parent_closed"
            and f"answered:{state.epoch}:{r['item']}:{r['seq']}" not in state.done
            and _closer_pane(r, state, panes) is not None]


def route(open_asks, closed, claims, ancestors, state: State, panes, humans) -> list:
    acts: list = []
    wakes: dict[str, list[tuple[str, str]]] = {}   # pane -> [(key, line)]
    for a in open_asks:
        if a["addressee_kind"] == "human":
            acts += _human(a, a["addressee"], state, panes, humans)
            continue
        gen = state.asks[a["id"]].gen if a["id"] in state.asks else 0
        sessions = [next((c["session"] for c in claims
                          if c["item"] == anc and c["actor"] == a["addressee"]), None)
                    for anc in ancestors.get(a["id"], [])]
        candidates = [s for s in sessions if s] + [_orchestrator(a["root"], state)]
        pane = next((p for s in candidates if (p := _pane_of(s, panes))), None)
        if pane is None:
            acts += _human(a, a["effective_owner"], state, panes, humans)
        elif pane.wakeable:
            wakes.setdefault(pane.pane_id, []).append(
                (f"wake:{a['id']}:{gen}:{pane.session}", f"{a['id']} \"{a['title']}\""))
    for row in closed:
        ask, data = row["item"], row["data"]
        acts.append(Clear(f"clear:{state.epoch}:{ask}:{row['seq']}", ask))
        if data.get("reason") == "parent_closed":
            continue
        p = _closer_pane(row, state, panes)
        if p and p.wakeable:                    # a busy pane holds the wake (still_pending)
            verb = "answered" if row["change"] == "ask.answered" else "withdrawn"
            why = data.get("outcome") or data.get("reason") or data.get("resolution")
            tail = " Run trk show %s." % ask if verb == "answered" else ""
            acts.append(Wake(f"answered:{state.epoch}:{ask}:{row['seq']}", p.pane_id,
                             f"[hx] {ask} {verb}: {why}.{tail}",
                             # only the asker's own pane: a fallback pane's item is another ask
                             needs_you=ask if verb == "answered" and p.session == row.get("asker_session") else None))
    for pane_id, items in wakes.items():
        todo = [(k, line) for k, line in items if k not in state.done]
        if todo:
            head = f"[hx] {len(todo)} asks for you: " if len(todo) > 1 else "[hx] ask for you: "
            acts.append(Wake("+".join(k for k, _ in todo), pane_id,
                             head + " · ".join(l for _, l in todo) + ". Run trk show <id>."))
    # A queue item is re-offered every pass: needs-you drops it when its pane dies, and
    # Channels.notify writes it only when the file is missing.
    return [a for a in acts if a.key not in state.done
            or (isinstance(a, Notify) and a.channel == "queue")]
