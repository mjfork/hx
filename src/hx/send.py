"""hx send: type one line into panes by the watcher's own rule (drafts.py). Each pane is sent the
line once its input box has been empty for drafts.QUIET_S; a draft, a box hx cannot see, or a box
not quiet within `wait` seconds is refused, never typed over. All panes are watched together.
"""
import time
from types import SimpleNamespace

from hx import drafts
from hx.herdr import HerdrError


UNLISTED = "herdr's pane list does not show it, so hx cannot tell whose session it is"


def _why(rec: dict | None) -> str:
    if rec is None or rec.get("unreadable_since") is not None:
        return "the input box cannot be read (a dialog or menu is open, it is not a Claude pane, or the pane is gone)"
    if rec.get("empty_since") is None:
        return "there is text in the input box"
    return f"the input box has not been empty for {drafts.QUIET_S} seconds"


def _sessions(herdr) -> dict[str, str | None] | None:
    try:
        return {p.pane_id: p.session for p in herdr.panes()}
    except HerdrError:
        return None


def send(herdr, panes: list[str], text: str, wait: float, clock=time.monotonic,
         sleep=time.sleep, still=None) -> dict[str, str]:
    """pane -> "sent" or "refused: <why>", in the order given. Each pane is read with its own
    timestamp and its current session, and nothing is typed once `wait` has passed. `still(pane,
    session)`, if given, runs right before typing; a reason it returns refuses that pane."""
    line = " ".join(text.split())
    state = SimpleNamespace(drafts={})
    todo = list(dict.fromkeys(panes))
    out: dict[str, str] = {}
    start = clock()
    unlisted: set[str] = set()
    while todo and clock() - start <= wait:
        sessions = _sessions(herdr)
        for p in list(todo):
            now = clock()
            if now - start > wait:
                break
            if sessions is None or p not in sessions:     # whose box is it? unknown, so hold it
                unlisted.add(p)
                state.drafts.pop(p, None)
                continue
            unlisted.discard(p)
            ok, _ = drafts.clear(herdr, p, state, now, sessions[p])
            if not ok or clock() - start > wait:
                continue
            again = _sessions(herdr)                      # the same session, right before typing
            if again is None or again.get(p, ...) != sessions[p]:
                state.drafts.pop(p, None)
                continue
            todo.remove(p)
            why = still(p, sessions[p]) if still else None
            if why:
                out[p] = f"refused: {why}"
                continue
            try:
                herdr.wake(p, line)
                out[p] = "sent"
            except HerdrError as e:
                out[p] = f"refused: herdr could not type into it ({e})"
        left = wait - (clock() - start)
        if todo and left > 0:
            sleep(min(drafts.QUICK_S, left))
        elif todo:
            break
    for p in todo:
        out[p] = f"refused: {UNLISTED if p in unlisted else _why(state.drafts.get(p))}"
    return {p: out[p] for p in dict.fromkeys(panes)}
