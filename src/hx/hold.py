"""A machine-wide hold: `hx hold` writes hold.json in hx's state folder, `hx resume` removes it.

The file is the contract every launcher reads on its own (launch-guard.sh,
claude-worker, trk's drivers, your own scripts): {"id", "reason", "by", "since", "until"}, times in UTC as
2027-01-15T08:00:00Z and until null for "until resumed". It is held when it exists and until is
null or later than now. A file that cannot be read counts as held: a reader never guesses free.

A script that holds and resumes on its own (a swap watcher) uses the checked forms, each a check and
a write under one lock, so it never overwrites or ends a hold someone else set between its read and
its write: put(if_free=True), put(replace=<id>) and resume(only=<id>). Each raises Mismatch when the
file is not what it expects. Never hold hold.json.lock around separate hx calls: hx takes it itself.

The watcher tells each live Claude pane once when a hold starts and once when it ends (wakes()).
Sessions that start during a hold hear it from claude/hooks/hold-notice.sh instead.
"""
import fcntl
import json
import math
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from hx.routing import Wake

KEYS = {"id", "reason", "by", "since", "until"}
DURATION = re.compile(r"^(\d+)([smhd])$")
# A time must name its zone: a reader that guessed local time would disagree with one that refused.
TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
MAX_BYTES = 65536        # a bigger file is unreadable, to hx and the hook alike
RESUME_KEEP_S = 3600     # a session away when the hold ends hears RESUME if back within this
SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class Mismatch(Exception):
    """A checked write found another hold, or an unreadable file, and changed nothing."""

    def __init__(self, status: "Status", wanted: str):
        self.status = status
        held = status.record or {}
        now = (f"hold {held.get('id')} by {held.get('by')}" if status.kind == "held" else
               f"an unreadable hold file ({status.error})" if status.kind == "unreadable" else "no hold")
        super().__init__(f"{wanted}, but there is {now}")


@dataclass(frozen=True)
class Status:
    kind: str                    # held | free | unreadable
    record: dict | None = None
    error: str = ""


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _time(text: str) -> float:
    """An ISO time with a zone (Z or +hh:mm) as epoch seconds."""
    if not TIME.match(text):
        raise ValueError(f"{text!r} is not an ISO time with a zone, like 2027-01-15T08:00:00Z")
    return datetime.fromisoformat(text).timestamp()


def parse_until(text: str, now: float) -> float:
    """`2h`, `30m`, `90s`, `1d`, or an ISO time with a zone, which must be later than now."""
    m = DURATION.match(text.strip())
    if m:
        at = now + int(m[1]) * SECONDS[m[2]]
    else:
        try:
            at = _time(text.strip())
        except ValueError:
            raise ValueError(f"{text!r} is neither a duration (2h, 30m) nor an ISO time with a zone "
                             "(2027-01-15T08:00:00Z)") from None
    if at <= now:
        raise ValueError(f"{text!r} is not later than now")
    return at


def _bytes(path: Path) -> bytes | None:
    """The file's bytes, or None when there is no file. Opened without blocking and checked on
    that same descriptor, so a FIFO swapped in never makes a reader wait."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        if os.path.lexists(path):
            raise OSError("it is a link to nothing") from None
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("it is not a regular file")
        data = b""
        while len(data) <= MAX_BYTES:           # read(2) may return early: read to the end
            chunk = os.read(fd, MAX_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
        if len(data) > MAX_BYTES:
            raise OSError(f"it is larger than {MAX_BYTES} bytes")
        return data
    finally:
        os.close(fd)


def read(path: Path, now: float) -> Status:
    try:
        raw = _bytes(path)
    except OSError as e:
        return Status("unreadable", error=str(e))
    if raw is None:
        return Status("free")
    try:
        rec = json.loads(raw.decode("utf-8"))   # UnicodeDecodeError is a ValueError
        if not isinstance(rec, dict) or not KEYS <= rec.keys():
            raise ValueError(f"it needs the keys {', '.join(sorted(KEYS))}")
        until = rec["until"]
        if until is not None and not isinstance(until, str):
            raise ValueError("until must be a time or null")
        expired = until is not None and _time(until) <= now
    except ValueError as e:                    # json.JSONDecodeError is one
        return Status("unreadable", error=str(e))
    return Status("free" if expired else "held", rec)


@contextmanager
def _locked(path: Path):
    """hx's writers and its expiry take turns; launchers only read, so they never wait."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def put(path: Path, reason: str, by: str, until: float | None, now: float, *,
        if_free: bool = False, replace: str | None = None) -> dict:
    """Write a new hold. if_free: only when no hold is live (an expired one is free; an unreadable
    file is not). replace: only when the file holds that id, live or expired. Either raises Mismatch."""
    rec = {"id": secrets.token_hex(4), "reason": " ".join(reason.split()), "by": by,
           "since": iso(now), "until": iso(math.ceil(until)) if until is not None else None}   # never earlier
    with _locked(path):
        if if_free or replace is not None:
            st = read(path, now)
            if if_free and st.kind != "free":
                raise Mismatch(st, "--if-free wants no live hold")
            if replace is not None and (st.kind == "unreadable" or (st.record or {}).get("id") != replace):
                raise Mismatch(st, f"--replace wants hold {replace}")
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(rec, indent=1) + "\n")
        os.replace(tmp, path)                  # a reader sees the old file or the new, never half
    return rec


def _remove(path: Path, now: float, only_expired: bool, only: str | None = None) -> dict | None:
    with _locked(path):
        st = read(path, now)
        if st.kind == "free" and st.record is None:
            return None
        if only is not None and (st.kind == "unreadable" or (st.record or {}).get("id") != only):
            raise Mismatch(st, f"--id wants hold {only}")
        if only_expired and not (st.kind == "free" and st.record is not None):
            return None
        if path.is_dir() and not path.is_symlink():
            path.rmdir()                       # only an empty folder; anything more is reported
        else:
            path.unlink(missing_ok=True)
        return st.record if st.record is not None else {}


def resume(path: Path, now: float | None = None, only: str | None = None) -> dict | None:
    """Remove the hold: its record, {} for a file that could not be read, None when there was none.
    only: remove it only when it is that hold (live or expired), else raise Mismatch."""
    return _remove(path, time.time() if now is None else now, only_expired=False, only=only)


def expire(path: Path, now: float) -> dict | None:
    """Remove the file once its until has passed, so a later reader finds nothing."""
    return _remove(path, now, only_expired=True)


def hold_text(rec: dict) -> str:
    until = f"until {rec['until']}" if rec["until"] else "until hx sends RESUME"
    return (f"HOLD (hx {rec['id']}) by {rec['by']} since {rec['since']}, {until}: {rec['reason']} "
            "Start no new sessions, builds, tests or deploys until the RESUME message.")


def resume_text(h: dict) -> str:
    return f"RESUME (hx {h['id']}): the hold by {h['by']} is over ({h['reason']}). Normal rules apply again."


def wakes(st: Status, state, panes, path: Path, now: float) -> tuple[list[Wake], list[str]]:
    """This pass's hold and resume wakes, and the inbox lines for a change. Both are told to
    sessions, not panes, once each (state.done). state.hold keeps the hold hx last saw, every
    session seen live while it held (each hears RESUME, though the hook may have been the one to
    tell it HOLD), and when it ended; resume debt ends after RESUME_KEEP_S."""
    seen = state.hold or {}
    live = [p for p in panes if p.session]
    if st.kind == "unreadable":                # held: sessions seen now are owed the resume too
        if not seen.get("id") or "ended" in seen:   # no hold known: stand in for the unread one
            seen = {"id": f"u{int(now)}", "by": "someone", "reason": "its file could not be read",
                    "heard": [], "unreadable": seen.get("unreadable", False)}
        heard = list(seen.get("heard", []))
        heard += [p.session for p in live if p.session not in heard]
        state.hold = {**seen, "heard": heard, "unreadable": True}
        if seen.get("unreadable"):
            return [], []
        return [], [f"HOLD  {path} cannot be read ({st.error}), so every launcher counts the "
                    "machine as held. Fix it, or remove it with `hx resume`."]
    if st.kind == "held":
        rec = st.record
        same = seen.get("id") == rec["id"] and "ended" not in seen
        heard = list(seen.get("heard", [])) if same else []
        heard += [p.session for p in live if p.session not in heard]
        state.hold = {"id": rec["id"], "by": rec["by"], "reason": rec["reason"], "heard": heard}
        lines = [] if same else [f"HOLD  {rec['id']}  by {rec['by']}: {rec['reason']}"]
        text = hold_text(rec)
        return [Wake(f"hold:{rec['id']}:{p.session}", p.pane_id, text) for p in live
                if p.wakeable and f"hold:{rec['id']}:{p.session}" not in state.done], lines
    if not seen.get("id"):
        state.hold = None
        return [], []
    hid, ended = seen["id"], seen.get("ended", now)
    lines = [] if "ended" in seen else [f"RESUMED  {hid}  the hold by {seen['by']} is over"]
    owed = {s for s in seen.get("heard", []) if f"resume:{hid}:{s}" not in state.done}
    if not owed or now - ended >= RESUME_KEEP_S:
        state.hold = None
        return [], lines
    state.hold = {**seen, "ended": ended, "unreadable": False}
    text = resume_text(seen)
    return [Wake(f"resume:{hid}:{p.session}", p.pane_id, text) for p in live
            if p.session in owed and p.wakeable], lines
