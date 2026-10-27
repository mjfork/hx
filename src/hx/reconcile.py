"""One pass: read trk and herdr, decide, act, then persist. Safe to repeat at any time."""
import copy
import time
from pathlib import Path

import json

from hx.channels import Channels
from hx import drafts, finish, hold, needs_you, sidebar
from hx.comments import Log, comments_owed, route_comments
from hx.config import Config, load_config
from hx.feed import FeedResult, advance
from hx.replies import replies_owed, route_replies
from hx.routing import Clear, Notify, Wake, route, still_pending
from hx.spaces import Close, MapRow, Note, Release, Report, Snapshot, decide, iso, root_of
from hx.showcache import ShowCache
from hx.state import load, locked, save
from hx.tracker import Trk, TrkError

REPLY_FRESH_S = 600     # an ask.replied row older than this when first read is history (hx was down)


def _ancestors(get, open_asks: list[dict]) -> dict[str, list[str]]:
    def parent_of(item: str) -> str | None:
        return get(item)["parent"]

    out = {}
    for a in open_asks:
        chain, cur = [], a["parent"]["id"]
        while cur:
            chain.append(cur)
            cur = parent_of(cur)
        out[a["id"]] = chain
    return out


def _catch_up(trk: Trk, state, history_before: str | None = None,
              fresh_after: str | None = None) -> FeedResult:
    """Read every page up to head. From seq 0 with no cutoff, it is history from end to end;
    after a restore, rows written before the last finished pass are history."""
    replay = state.seq == 0 and history_before is None
    total = FeedResult()
    while True:
        reply = trk.changes(state.seq)
        r = advance(state, reply, replay=replay, history_before=history_before, fresh_after=fresh_after)
        if r.rebuilt:
            return r
        total.closed += r.closed
        total.touched |= r.touched
        total.replied += r.replied
        total.closes.update(r.closes)
        total.comments += r.comments
        total.acked |= r.acked
        if not reply["changes"] or reply["next"] >= reply["head"]:
            return total


def _owed(closed: list[dict], state, panes) -> list[dict]:
    """Closed rows not finished: their clear is not done, or their wake is still owed."""
    return [r for r in closed
            if f"clear:{state.epoch}:{r['item']}:{r['seq']}" not in state.done
            or r in still_pending([r], state, panes)]


def _cutoff(t: float) -> str:
    """An ISO time with milliseconds, as trk writes `at`, so the two compare as strings."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t * 1000) % 1000:03d}Z"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _items(show, claims: list[dict], bindings: dict, log) -> dict[str, dict]:
    """trk show for every claimed item, its root and every bound root. An item that cannot be shown
    is left out (decide skips it), so one bad item never stops the pass."""
    items: dict[str, dict] = {}

    def get(i: str) -> dict | None:
        if i not in items:
            try:
                items[i] = show(i)
            except TrkError as e:
                log(f"space: {e}")
                return None
        return items[i]

    for c in claims:
        it = get(c["item"])
        if it is not None and get(root_of(it)) is None:
            items.pop(c["item"], None)
    for root in bindings:
        get(root)
    return items


def _parent(show, log):
    """The item's parent id, or None at a root or when trk cannot show it (the walk then stops)."""
    def parent(item: str) -> str | None:
        try:
            return show(item)["parent"]
        except TrkError as e:
            log(f"comment: {e}")
            return None
    return parent


def _logger(path: Path):
    def log(text: str) -> None:
        with path.open("a") as f:
            f.write(f"{_now()}  ERROR  {text}\n")
    return log


def _act_space(e, herdr, trk: Trk, inbox: Path, sessions_log: Path, state, now: float) -> None:
    if isinstance(e, Report):
        (herdr.report_space if e.kind == "ws" else herdr.report_pane)(e.target, e.value)
        key = e.key.split(":", 1)[1]
        if e.value is None:
            state.reported.pop(key, None)
        else:
            state.reported[key] = [e.value, now]
        return
    if isinstance(e, Close):
        herdr.close_space(e.workspace)
        with inbox.open("a") as f:
            f.write(f"{iso(now)}  CLOSED  {e.root}  Space {e.workspace}\n")
    elif isinstance(e, Release):
        trk.release(e.session)
        with inbox.open("a") as f:
            f.write(f"{iso(now)}  RELEASED  {e.session}  its claims: the session left herdr\n")
    elif isinstance(e, MapRow):
        with sessions_log.open("a") as f:
            f.write(json.dumps(e.row) + "\n")
        state.sessions[e.session] = [e.item, e.role]
    elif isinstance(e, Note):
        with inbox.open("a") as f:
            f.write(f"{iso(now)}  {e.text}\n")
    state.done[e.key] = iso(now)


def reconcile(trk: Trk, herdr, state_path: Path, inbox: Path, queue_dir: Path,
              humans: dict, dry_run: bool = False, cfg: Config | None = None,
              now: float | None = None, cache: ShowCache | None = None) -> list:
    """`cache` carries trk show results from pass to pass; without one, every item is read fresh."""
    with locked(state_path):
        return _reconcile(trk, herdr, state_path, inbox, queue_dir, humans, dry_run,
                          cfg or load_config(Path("/nonexistent")), now, cache or ShowCache())


def _read_feed(trk, state, cache: ShowCache, fresh_after: str) -> FeedResult:
    result = _catch_up(trk, state, fresh_after=fresh_after)
    if result.rebuilt:
        cache.clear()
        state.pending, state.replies, state.comments = [], [], []
        result = _catch_up(trk, state, history_before=state.last_pass_at, fresh_after=fresh_after)
    cache.forget(result.touched)           # before anything can fail and save the new cursor
    return result


def _reconcile(trk, herdr, state_path, inbox, queue_dir, humans, dry_run, cfg, now, cache) -> list:
    state = copy.deepcopy(load(state_path))
    now = time.time() if now is None else now
    fresh_after = _cutoff(now - REPLY_FRESH_S)
    result = _read_feed(trk, state, cache, fresh_after)
    show = lambda i: cache.get(trk, i, now)  # noqa: E731
    log = _logger(state_path.with_name("watch.log"))
    open_asks = _waiting(trk, herdr, state_path, queue_dir, now, log, dry_run)
    root_now = {a["id"]: a["root"] for a in open_asks}
    replied = [{**r, "root": root_now[r["item"]]}            # the ask may have moved since
               for r in state.replies + result.replied     # a later close ends its replies
               if r["seq"] > result.closes.get(r["item"], -1) and r["item"] in root_now]
    claims = trk.claims()["claims"]
    panes = herdr.panes()
    closed = state.pending + result.closed
    comments = [r for r in state.comments + result.comments if r["data"]["comment_id"] not in result.acked]
    parent = _parent(show, log)

    def owed():
        state.pending = _owed(closed, state, panes)
        state.replies = replies_owed(replied, state, panes, claims, parent, fresh_after)
        state.comments = comments_owed(comments, state, panes, claims, parent, fresh_after)

    hold_path = state_path.with_name("hold.json")
    if not dry_run:
        hold.expire(hold_path, now)
    hold_wakes, hold_lines = hold.wakes(hold.read(hold_path, now), state, panes, hold_path, now)
    actions = hold_wakes + route(open_asks, closed, claims, _ancestors(show, open_asks),
                                 state, panes, humans) + route_replies(replied, state, panes, claims, parent, fresh_after) \
        + route_comments(comments, state, panes, claims, parent, fresh_after)
    if dry_run:
        snap = Snapshot(claims, _items(show, claims, state.bindings, log), open_asks, panes,
                        herdr.workspaces(), now)
        return actions + decide(snap, state, cfg).effects
    channels = Channels(inbox, queue_dir, {p.pane_id: p.workspace_id for p in panes})
    channels.sweep({a["id"] for a in open_asks})
    for line in hold_lines:
        channels.log(line)
    woken: set[str] = set()     # one wake per pane per pass: the snapshot is stale after the first
    held: set[str] = set()      # panes with a person's draft (or no readable box) this pass
    sessions = {p.pane_id: p.session for p in panes}
    try:
        for a in actions:
            if isinstance(a, Wake) and (a.pane in woken or a.pane in held):
                continue                       # still owed, so the next pass sends it
            if isinstance(a, Wake):
                ok, line = drafts.clear(herdr, a.pane, state, now, sessions.get(a.pane))
                if line:
                    channels.log(line)
                if not ok:
                    held.add(a.pane)
                    owed()                     # the rows this pass read are saved as owed
                    save(state, state_path)
                    continue
            if isinstance(a, Notify):
                ok = channels.notify(a)
            elif isinstance(a, Clear):
                channels.clear(a)
                ok = True
            elif isinstance(a, Wake):
                herdr.wake(a.pane, a.text)
                woken.add(a.pane)
                state.drafts.pop(a.pane, None)  # the next wake watches the box afresh
                ok = True
            elif isinstance(a, Log):
                channels.log(a.line)
                ok = True
            if ok:
                for part in a.key.split("+"):
                    state.done[part] = _now()
                owed()
                save(state, state_path)        # each action is recorded before the next one
                if isinstance(a, Wake) and a.needs_you:
                    try:                       # after the save: a crash never sends, and clears, twice
                        needs_you.clear(a.pane, a.needs_you)
                    except Exception as e:     # the queue is herdr-jump's: never stop the pass for it
                        log(f"needs-you: {a.pane}: {e}")
    except BaseException:
        owed()
        save(state, state_path)
        raise
    owed()
    drafts.keep_held(state, held)
    state.pending = still_pending(closed, state, panes)
    seen_before = dict(state.seen)
    effects = _spaces(trk, show, herdr, state, state_path, inbox, claims, open_asks, panes, cfg, now,
                      log)
    if panes:                                  # without a snapshot, nobody has left
        try:
            finish.remember(state, panes)
            finish.queue(state, seen_before, now)
            lines, errors = finish.run(state, trk, panes, cfg.finish, now)
            for line in lines:
                channels.log(line)
            for err in errors:
                log(f"finish: {err}")
        except Exception as e:
            log(f"finish: {type(e).__name__}: {e}")
    for err in _sidebar(herdr, state, state_path, queue_dir, open_asks, panes, now):
        log(f"sidebar: {err}")
    state.last_pass_at = _cutoff(time.time())   # milliseconds, as trk's `at`
    save(state, state_path)
    return actions + effects


def _waiting(trk, herdr, state_path, queue_dir, now, log, dry_run=False) -> list[dict]:
    """trk's open asks for a pass (its caller holds the state lock). An ask that breaks the contract
    is logged and left out. When trk fails outright, the panel is still drawn from the queue files,
    marked trk-down, before the error stops the pass: only the panel's field of the saved state
    changes, so the pass's unsaved feed cursor is not kept."""
    try:
        reply = trk.waiting()
    except Exception as e:
        if dry_run:
            raise
        try:
            panes = herdr.panes()
        except Exception:
            raise e from None
        saved = load(state_path)
        for err in _sidebar(herdr, saved, state_path, queue_dir, [], panes, now,
                            trk_error=f"{type(e).__name__}: {e}"):
            log(f"sidebar: {err}")
        save(saved, state_path)
        raise
    for bad in () if dry_run else reply.get("invalid", ()):
        log(f"trk waiting: ask {bad['id']} left out, it breaks the contract: {bad['error']}")
    return reply["asks"]


def _sidebar(herdr, state, state_path, queue_dir, open_asks, panes, now, trk_error=None) -> list[str]:
    """Draw the needs-you panel; display only, so it never raises. Returns the errors. `trk_error`
    marks trk's asks as unavailable: the rows then come from the queue files alone."""
    try:
        live = {p.pane_id for p in panes}
        rows = sidebar.rows(queue_dir, sidebar.cache_dir(), open_asks, live, now,
                            sidebar.blocked(panes, sidebar.mode_dir()),
                            {p.pane_id for p in panes if p.status == "working"})
        sidebar.write_line(state_path.with_name("sidebar.json"), rows, now, trk_error)
        state.sidebar, errors = sidebar.publish(herdr, rows, state.sidebar, live)
        return errors
    except Exception as e:
        return [f"{type(e).__name__}: {e}"]


def refresh_sidebar(trk, herdr, state_path: Path, queue_dir: Path, now: float) -> list[str]:
    """The panel alone, between passes: `hx watch` runs it when the queue folder changes, so a row
    goes the moment herdr-jump's prompt hook deletes its item. It reads trk's open asks and herdr's
    panes afresh and saves only the panel's field. Returns the errors. A failed herdr read changes nothing; a failed trk
    read draws the queue-file rows alone, marked trk-down."""
    try:
        panes = herdr.panes()
    except Exception as e:
        return [f"{type(e).__name__}: {e}"]
    errors, trk_error = [], None
    try:
        reply = trk.waiting()
        open_asks = reply["asks"]
        errors += [f"trk waiting: ask {b['id']} left out, it breaks the contract: {b['error']}"
                   for b in reply.get("invalid", ())]
    except Exception as e:                     # the queue files still make rows
        open_asks, trk_error = [], f"{type(e).__name__}: {e}"
        errors.append(trk_error)
    with locked(state_path):                   # `hx start` may write state.json meanwhile
        state = load(state_path)
        errors += _sidebar(herdr, state, state_path, queue_dir, open_asks, panes, now, trk_error)
        save(state, state_path)
    return errors


def _spaces(trk, show, herdr, state, state_path, inbox, claims, open_asks, panes, cfg, now,
            log) -> list:
    """The Space half of a pass. It runs after ask routing and never stops it: a failure here is
    logged, and each effect that fails is retried on a later pass."""
    try:
        snap = Snapshot(claims, _items(show, claims, state.bindings, log), open_asks, panes,
                        herdr.workspaces(), now)
        plan = decide(snap, state, cfg)
    except Exception as e:
        log(f"space: {type(e).__name__}: {e}")
        return []
    sessions_log = state_path.with_name("sessions.jsonl")
    for e in plan.effects:
        try:
            _act_space(e, herdr, trk, inbox, sessions_log, state, now)
        except Exception as err:
            log(f"space: {type(e).__name__} {e.key}: {err}")
            if isinstance(e, Close):              # still open: keep its binding, try again later
                plan.bindings[e.root] = state.bindings[e.root]
                plan.forget = [k for k in plan.forget if k != f"ws:{e.workspace}"
                               and not k.startswith(f"pane:{e.workspace}:")]
            elif isinstance(e, Release):          # still held: keep watching the session
                plan.seen[e.session] = state.seen[e.session]
            continue
        save(state, state_path)
    state.bindings, state.seen, state.absent = plan.bindings, plan.seen, plan.absent
    for key in plan.forget:
        state.reported.pop(key, None)
    return plan.effects
