"""hx reconcile [--dry-run] · hx watch · hx hold / resume / held · hx send · hx needs-you --line / --json · hx answer ·
hx sidebar setup: the commands systemd, herdr and people run."""
import argparse
import difflib
import getpass
import json
import math
import os
import shutil
import sys
import time
from pathlib import Path

from hx import asks, drafts, herdr_config, hold, sidebar
from hx.config import config_path, load_config
from hx.feed import SchemaChanged
from hx.herdr import Herdr, HerdrError
from hx.reconcile import reconcile, refresh_sidebar
from hx.send import send
from hx.showcache import ShowCache
from hx.start import StartError, start
from hx.state import load, state_dir
from hx.tracker import Trk, TrkError

QUEUE_DIR = Path.home() / ".herdr-queue" / os.environ.get("HERDR_SESSION", "default")


def feed_head_path() -> Path:
    home = os.environ.get("TRK_HOME")
    return (Path(home) if home else Path.home() / ".local/share/trk") / "feed.head"


def _pass(dry_run: bool, cache: ShowCache | None = None) -> list:
    d = state_dir()
    cfg = load_config(config_path())
    return reconcile(Trk(contract_dir=cfg.trk_contract), Herdr(), d / "state.json", d / "inbox.log", QUEUE_DIR,
                     cfg.humans, dry_run=dry_run, cfg=cfg, cache=cache)


def watch_once(last, path: Path, run, now: float, last_run: float, tick: float):
    mtime = path.stat().st_mtime if path.exists() else None
    if mtime != last or now - last_run >= tick:
        run()
        return mtime, now
    return mtime, last_run


def watch_queue(last: tuple | None, folder: Path, refresh) -> tuple | None:
    """herdr-jump adds and deletes the queue's items (a deleted one: the pane's prompt was
    submitted). On a change to the items since the last look, redraw the panel. Only *.json counts:
    herdr-jump rewrites its own dot files there on every footer poll. The first look only records
    the items. Returns what to pass next time."""
    try:
        now = tuple(sorted((f.name, f.stat().st_mtime_ns) for f in folder.glob("*.json")))
    except OSError:
        return last
    if last is not None and now != last:
        refresh()
    return now


def _refresh() -> None:
    d = state_dir()
    cfg = load_config(config_path())
    for err in refresh_sidebar(Trk(contract_dir=cfg.trk_contract), Herdr(),
                               d / "state.json", QUEUE_DIR, time.time()):
        with (d / "watch.log").open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  sidebar refresh: {err}\n")


def handle_error(e: Exception, log: Path, inbox: Path, alerted: bool) -> bool:
    """Log every error. A contract or schema break means the watcher has stopped working:
    say so once in the console inbox. Returns whether that alert is now out."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with log.open("a") as f:
        f.write(f"{stamp}  ERROR  {type(e).__name__}: {e}\n")
    broken = isinstance(e, SchemaChanged) or (isinstance(e, TrkError) and "breaks contract" in str(e))
    if broken and not alerted:
        with inbox.open("a") as f:
            f.write(f"{stamp}  ALERT  hx watcher stopped acting: {e}\n")
    return alerted or broken


def _tick(state_path: Path, tick: float) -> float:
    """The pass interval: QUICK_S while a pane waits out its quiet period, so hx keeps seeing
    the box (drafts.py); otherwise the normal tick."""
    try:
        return min(drafts.QUICK_S, tick) if drafts.counting(load(state_path)) else tick
    except (OSError, ValueError):
        return tick


def _sleep_for(interval: float, now_tick: float) -> float:
    """A quick tick shortens the poll sleep, so a long --interval cannot space sightings out."""
    return min(interval, now_tick)


def _watch(interval: float, tick: float) -> int:
    d = state_dir()
    last, last_run, backoff, alerted = None, 0.0, 1.0, False
    qlast = None
    cache = ShowCache()                    # trk show results, kept from pass to pass
    now_tick = tick
    while True:
        try:
            ran = last_run
            last, last_run = watch_once(last, feed_head_path(), lambda: _pass(False, cache),
                                        time.time(), last_run, now_tick)
            if last_run != ran:
                now_tick = _tick(d / "state.json", tick)
                qlast = watch_queue(None, QUEUE_DIR, None)     # the pass drew the panel already
            else:
                qlast = watch_queue(qlast, QUEUE_DIR, _refresh)
            backoff, alerted = 1.0, False
            time.sleep(_sleep_for(interval, now_tick))
        except Exception as e:
            alerted = handle_error(e, d / "watch.log", d / "inbox.log", alerted)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


def _positive(text: str) -> float:
    v = float(text)
    if not math.isfinite(v) or v <= 0:
        raise argparse.ArgumentTypeError(f"must be more than 0, not {text}")
    return v


def _hold(args, path: Path, now: float) -> int:
    if args.cmd == "hold":
        try:
            until = hold.parse_until(args.until, now) if args.until else None
        except ValueError as e:
            print(f"hx hold: --until {e}", file=sys.stderr)
            return 2
        rec = hold.put(path, args.reason, args.by or getpass.getuser(), until, now)
        print(f"held: {rec['id']} until {rec['until'] or 'hx resume'}. Live agents hear it on the "
              f"watcher's next pass; launchers refuse new sessions now. ({path})")
        return 0
    if args.cmd == "resume":
        try:
            rec = hold.resume(path, now)
        except OSError as e:
            print(f"hx resume: cannot remove {path}: {e}", file=sys.stderr)
            return 2
        print("no hold to resume" if rec is None else
              f"resumed {rec.get('id', '(unreadable file removed)')}: live agents hear it on the watcher's next pass")
        return 0
    st = hold.read(path, now)
    if args.json:
        print(json.dumps({"held": st.kind != "free", "kind": st.kind, "record": st.record,
                          "error": st.error or None, "path": str(path)}))
    elif st.kind == "held":
        r = st.record
        print(f"held: {r['reason']} (by {r['by']} since {r['since']}, until {r['until'] or 'hx resume'})")
    elif st.kind == "unreadable":
        print(f"held: {path} cannot be read ({st.error})")
    else:
        print("not held")
    return {"held": 0, "free": 1, "unreadable": 2}[st.kind]


def _sidebar_setup(config: Path, dry_run: bool) -> int:
    hx_bin = shutil.which("hx") or sys.argv[0]
    try:
        text = config.read_text() if config.exists() else ""
        new = herdr_config.edit(text, hx_bin)
        if new == text:
            print(f"{config} already shows the hx sidebar; nothing to change")
            return 0
        if dry_run:
            sys.stdout.writelines(difflib.unified_diff(text.splitlines(True), new.splitlines(True),
                                                       str(config), f"{config} (hx)"))
            return 0
        backup = herdr_config.apply(config, hx_bin, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    except (OSError, herdr_config.ConfigError) as e:
        print(f"hx sidebar setup: {e}", file=sys.stderr)
        return 1
    print(f"updated {config} (backup: {backup}). Load it with: herdr server reload-config")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hx")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reconcile")
    r.add_argument("--dry-run", action="store_true")
    st = sub.add_parser("start")
    st.add_argument("root")
    st.add_argument("--model")
    st.add_argument("--effort", default="medium")
    st.add_argument("--reason")
    st.add_argument("--dry-run", action="store_true")
    w = sub.add_parser("watch")
    w.add_argument("--interval", type=_positive, default=1.0)
    w.add_argument("--tick", type=_positive, default=30.0)
    h = sub.add_parser("hold", help="hold the whole machine: launchers refuse to start sessions")
    h.add_argument("--reason", required=True)
    h.add_argument("--until", help="2h, 30m, 1d or an ISO time; default: until hx resume")
    h.add_argument("--by", default=None, help="who holds (default: your user name)")
    sub.add_parser("resume", help="end the hold")
    hd = sub.add_parser("held", help="exit 0 if held, 1 if not, 2 if hold.json cannot be read")
    hd.add_argument("--json", action="store_true")
    sd = sub.add_parser("send", help="type a line into panes, never over a person's draft")
    sd.add_argument("--pane", action="append", required=True, help="a herdr pane id; repeat for more")
    sd.add_argument("--wait", type=_positive, default=45.0,
                    help=f"seconds to wait for each box to be empty for {drafts.QUIET_S} s (default 45)")
    sd.add_argument("text")
    ny = sub.add_parser("needs-you", help="the needs-you queue: the tab bar's count, or the cards as JSON")
    nyw = ny.add_mutually_exclusive_group(required=True)
    nyw.add_argument("--line", action="store_true", help="the tab bar's text (herdr runs it every 15 s)")
    nyw.add_argument("--json", action="store_true", help="one card per live pane's item, its asks parsed")
    an = sub.add_parser("answer", help="type a reply into a needs-you ask, only if it is the one shown")
    an.add_argument("pane")
    an.add_argument("--sha", required=True, help="the card's sha: the ask text it showed")
    an.add_argument("--session", required=True, help="the card's session: the one that asked")
    an.add_argument("--wait", type=_positive, default=45.0,
                    help=f"seconds to wait for the box to be empty for {drafts.QUIET_S} s (default 45)")
    an.add_argument("text")
    sb = sub.add_parser("sidebar", help="herdr's Agents panel as the needs-you list")
    sb_sub = sb.add_subparsers(dest="sidebar_cmd", required=True)
    setup = sb_sub.add_parser("setup", help="set herdr's sidebar rows, width and tab bar for hx (backs up first)")
    setup.add_argument("--dry-run", action="store_true", help="print the change, write nothing")
    setup.add_argument("--config", type=Path, default=Path.home() / ".config/herdr/config.toml")
    args = p.parse_args(argv)
    if args.cmd == "needs-you" and args.json:
        cfg = load_config(config_path())
        try:
            open_asks = Trk(contract_dir=cfg.trk_contract).waiting()["asks"]
            out = asks.cards(Herdr(), QUEUE_DIR, sidebar.cache_dir(), open_asks, time.time())
        except Exception as e:                   # the page shows this; never a traceback
            print(f"hx needs-you: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        print(json.dumps({"schema": 1, "cards": out}))
        return 0
    if args.cmd == "needs-you":
        print(sidebar.line(state_dir() / "sidebar.json", time.time()))
        return 0
    if args.cmd == "answer":
        if args.wait < drafts.QUIET_S:
            print(f"hx answer: --wait must be at least {drafts.QUIET_S} (the quiet window)", file=sys.stderr)
            return 2
        r = asks.answer(Herdr(), QUEUE_DIR, args.pane, args.sha, args.session, args.text, args.wait)
        print(f"sent  {args.pane}" if r == "sent" else f"refused  {args.pane}  {r.removeprefix('refused: ')}")
        return 0 if r == "sent" else 1
    if args.cmd == "sidebar":
        return _sidebar_setup(args.config, args.dry_run)
    if args.cmd == "send":
        if args.wait < drafts.QUIET_S:
            print(f"hx send: --wait must be at least {drafts.QUIET_S} (the quiet window)", file=sys.stderr)
            return 2
        result = send(Herdr(), args.pane, args.text, args.wait)
        for pane, r in result.items():
            print(f"sent  {pane}" if r == "sent" else f"refused  {pane}  {r.removeprefix('refused: ')}")
        return 0 if all(r == "sent" for r in result.values()) else 1
    if args.cmd in ("hold", "resume", "held"):
        return _hold(args, state_dir() / "hold.json", time.time())
    if args.cmd == "reconcile":
        for a in _pass(args.dry_run):
            target = getattr(a, "pane", None) or getattr(a, "person", None) or getattr(a, "ask", "")
            print(f"{type(a).__name__} {a.key} {target}")
        return 0
    if args.cmd == "start":
        st = hold.read(state_dir() / "hold.json", time.time())
        if st.kind != "free":
            why = st.record["reason"] if st.record else f"hold.json cannot be read ({st.error})"
            print(f"hx start: the machine is held: {why}. `hx held` says more; it ends with `hx resume`.",
                  file=sys.stderr)
            return 1
        cfg = load_config(config_path())
        try:
            print(start(Trk(contract_dir=cfg.trk_contract), Herdr(), state_dir() / "state.json", cfg, args.root,
                        model=args.model, effort=args.effort, reason=args.reason,
                        parent=os.environ.get("TRK_SESSION") or None, dry_run=args.dry_run))
        except (StartError, TrkError, HerdrError) as e:
            print(f"hx start: {e}", file=sys.stderr)
            return 1
        return 0
    return _watch(args.interval, args.tick)


if __name__ == "__main__":
    sys.exit(main())
