"""A worker's finished turn wakes its driver (upstream ask UP-0006). Each pass notes every session's herdr status;
a worker that was working and is now idle or done has finished a turn, and its driver is owed one `[done]` line.
The driver is the session that started the worker (the parent hx recorded), else the session holding the root's
claim (the orchestrator first). A driver that is working holds the line; one that is gone gets none."""
from typing import Callable

from hx.routing import Wake, _pane_of

FINISHED = ("idle", "done")
KEEP_S = 30 * 60      # a line still owed after this, its driver busy all along, is dropped: the driver's cycle saw it


def _driver(session: str, root: str, state, claims: list[dict]) -> str | None:
    launched = next((b.get("launch") or {} for b in state.bindings.values() if b.get("launched") == session), {})
    if launched.get("parent"):
        return launched["parent"]
    held = [c for c in claims if c["item"] == root and c["session"] != session]
    held.sort(key=lambda c: c["actor"] != "orchestrator")
    return held[0]["session"] if held else (state.bindings.get(root) or {}).get("orchestrator")


def observe(state, panes, claims: list[dict], root_of: Callable[[str], str], now: float) -> None:
    """Note this pass's statuses, and owe a line for each worker whose turn just ended under a live driver."""
    live = {p.session: p for p in panes if p.session}
    for session, p in live.items():
        was = state.turns.get(session) or {"status": None, "n": 0}
        n = was["n"]
        mapped = state.sessions.get(session) or [None, None]
        if was["status"] == "working" and p.status in FINISHED and mapped[1] == "worker" and mapped[0]:
            n += 1
            item = mapped[0]
            driver = _driver(session, root_of(item), state, claims)
            if driver and driver != session and driver in live:
                state.finished.append({"key": f"done:{session}:{n}", "pane": p.pane_id, "item": item,
                                       "driver": driver, "at": now})
        state.turns[session] = {"status": p.status, "n": n}
    for session in [s for s in state.turns if s not in live]:
        del state.turns[session]
    state.finished = [f for f in state.finished
                      if f["key"] not in state.done and f["driver"] in live and now - f["at"] < KEEP_S]


def wakes(state, panes) -> list[Wake]:
    """One line per idle driver pane, naming each worker that finished; a busy driver's lines wait."""
    by_pane: dict[str, list[dict]] = {}
    for f in state.finished:
        p = _pane_of(f["driver"], panes)
        if p and p.wakeable:
            by_pane.setdefault(p.pane_id, []).append(f)
    out = []
    for pane, fs in by_pane.items():
        if len(fs) == 1:
            f = fs[0]
            text = f"[hx] {f['pane']} ({f['item']}) finished its turn. Run trk show {f['item']}."
        else:
            text = (f"[hx] {len(fs)} workers finished their turns: "
                    + ", ".join(f"{f['pane']} ({f['item']})" for f in fs) + ". Run trk show <id> for each.")
        key = "+".join(f["key"] for f in fs)
        out.append(Wake(key, pane, text, kind="done", ref=key))
    return out
