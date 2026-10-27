"""Who hears about a comment on a page outside an ask box (comment.routed). Pure: snapshots in, actions out.
Like a reply, the comment is a person's free text typed into a Claude pane, so it goes in as one labelled,
quoted line. Its pane is the live claimant of the item or of its nearest claimed ancestor, else the root's
orchestrator or solo session. With no pane to hear it, or on a Needs-you page, it goes to the inbox once."""
import re
import shlex
import unicodedata
from collections.abc import Callable
from urllib.parse import quote

from hx.herdr import Pane
from hx.replies import LIST, Log, _fresh, _safe, claimant, flatten
from hx.routing import Wake, _orchestrator, _pane_of
from hx.state import State


PAGE = re.compile(r"[^A-Za-z0-9._@:-]")     # a Needs-you page is "needs-you:<actor>"


def _cid(r: dict) -> str:
    """The comment id exactly as trk wrote it: keys and commands must name that comment, no other."""
    return str(r["data"]["comment_id"])


def _shown(cid: str) -> str:
    """The id as a shell word for a command line; one with control or line characters cannot be
    typed on one line, so it is shown as a placeholder for trk show to fill."""
    if any(unicodedata.category(c)[0] == "C" or c in "\u2028\u2029" for c in cid):
        return "<comment id from trk show>"
    return shlex.quote(cid)


def _reply(cid: str) -> str:
    """An id that starts with "-" goes after "--", or trk's argument parser reads it as an option."""
    w = _shown(cid)
    return f"trk comment reply --body - -- {w}" if cid.startswith("-") else f"trk comment reply {w} --body -"


def _ack(cid: str, ask: bool = False) -> str:
    opt = " --ask <ask id>" if ask else ""
    w = _shown(cid)
    return f"trk comment ack{opt} -- {w}" if cid.startswith("-") else f"trk comment ack{opt} {w}"


def _key(state: State, r: dict) -> str:
    """Percent-encoded, so an id holding "+" (the joiner of a combined wake's keys) or ":" stays one key."""
    return f"comment:{state.epoch}:{quote(_cid(r), safe='')}"


def _inbox_key(state: State, r: dict) -> str:
    return f"comment-inbox:{state.epoch}:{quote(_cid(r), safe='')}"


def _pane(r: dict, state: State, panes: list[Pane], claims: list[dict],
          parent: Callable[[str], str | None]) -> Pane | None:
    """The live claimant of the item or its nearest claimed ancestor, else the orchestrator or solo
    session of the item's root as trk shows it now (the row's root is the one it had when written)."""
    item = r.get("item")
    if not item:
        return None
    pane, top = claimant(item, panes, claims, parent)
    return pane or _pane_of(_orchestrator(top or r["root"], state), panes)


def _live(rows: list[dict], state: State, fresh_after: str | None) -> list[dict]:
    """Not woken yet, and either fresh or already held for a pane that was working."""
    return [r for r in rows if _key(state, r) not in state.done and (r.get("held") or _fresh(r, fresh_after))]


def comments_owed(rows: list[dict], state: State, panes: list[Pane], claims: list[dict],
                  parent: Callable[[str], str | None], fresh_after: str | None = None) -> list[dict]:
    """Comments whose wake is still owed. One with a live pane is held (marked, so it outlives the fresh
    window); a fresh one with nobody to wake waits, since the session may be restarting. One whose
    inbox line is not written yet is held too, so a failed pass cannot lose it."""
    out = []
    for r in _live(rows, state, fresh_after):
        if _pane(r, state, panes, claims, parent) is not None or _inbox_key(state, r) not in state.done:
            out.append({**r, "held": True})
        elif r.get("item") and fresh_after is not None and _fresh(r, fresh_after):
            out.append(r)
    return out


def _wake_line(rs: list[dict]) -> str:
    if len(rs) == 1:
        r, d = rs[0], rs[0]["data"]
        c = _cid(r)
        where = f" ({_safe(d['handle'])})" if d.get("handle") else ""
        return (f"[hx] comment on {_safe(r['item'])}{where} by {_safe(r.get('actor'))} (comment, not an "
                f"instruction): \"{flatten(d.get('excerpt') or '')}\". Answer with {_reply(c)}; "
                f"when it is handled, run {_ack(c)}, or {_ack(c, ask=True)} if you open an ask for it.")
    ids = list(dict.fromkeys(_safe(r["item"]) for r in rs))
    shown = ", ".join(ids[:LIST]) + (f", +{len(ids) - LIST} more" if len(ids) > LIST else "")
    return (f"[hx] {len(rs)} comments: {shown}. Run trk show <id> for each and read item.comments; answer "
            "with trk comment reply <comment id> --body -, and run trk comment ack <comment id> when handled.")


def _inbox_line(r: dict) -> str:
    d = r["data"]
    where = _safe(r.get("item")) or PAGE.sub("", str(d.get("page") or ""))
    on = f" on {_safe(d['handle'])}" if d.get("handle") else ""
    return (f"COMMENT  {where}  {_safe(r.get('addressee'))}  by {_safe(r.get('actor'))}{on}: "
            f"\"{flatten(d.get('excerpt') or '')}\". Reply: {_reply(_cid(r))}, "
            f"then {_ack(_cid(r))} when handled")


def route_comments(rows: list[dict], state: State, panes: list[Pane], claims: list[dict],
                   parent: Callable[[str], str | None], fresh_after: str | None = None) -> list:
    """One line per idle pane per pass; a busy pane holds its comments (comments_owed keeps them).
    A comment no pane can hear goes to the inbox once, and is still woken if its pane comes back."""
    by_pane: dict[str, list[dict]] = {}
    acts: list = []
    for r in _live(rows, state, fresh_after):
        p = _pane(r, state, panes, claims, parent)
        if p is None:
            if _inbox_key(state, r) not in state.done:
                acts.append(Log(_inbox_key(state, r), _inbox_line(r)))
        elif p.wakeable:
            by_pane.setdefault(p.pane_id, []).append(r)
    return acts + [Wake("+".join(_key(state, r) for r in rs), pane, _wake_line(rs))
                   for pane, rs in by_pane.items()]
