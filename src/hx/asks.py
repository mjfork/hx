"""The needs-you queue as cards, for a page that answers asks with a tap: `hx needs-you --json`
and `hx answer`. A card is one live pane's item (herdr-jump's queue) with its "## ⚑ Needs you"
section parsed into asks; hx's own hx_<ask>.json items are left out, because trk shows those asks.

The parser is strict: a section off the async turn format (Ask / Options / Reply with / Unblocks)
gives no asks, and the page shows the raw text. A reply is tappable only when it is a literal one:
never one that starts with "!" (Claude runs it as a shell command) or one with a <placeholder>.

`answer` types a reply only into the ask the card showed: the item must still be there with the
same text (its sha), the pane must run the same session, and both are checked again right before
send types. Each ask is answered once (a double tap, a retry). A reply that starts with "!" or "/",
or holds a control character, is refused outright: a person types those in the pane.
"""
import fcntl
import hashlib
import json
import math
import re
import time
import unicodedata
from pathlib import Path

from hx import sidebar
from hx.send import send
from hx.state import state_dir

ANSWERED_KEEP_S = 86400      # how long hx remembers that it answered an ask

FIELD = re.compile(r"^\*\*(Ask(?: (\d+))?|Options|Reply with|Unblocks):\*\*\s*(.*)$")
OPTION = re.compile(r"^(\d+)\.\s+(.*)$")
LITERAL = re.compile(r"`([^`]+)`")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _shell(reply: str) -> bool:
    return reply.lstrip().startswith("!")


def _unsafe(reply: str) -> str | None:
    """Why hx never types this reply: Claude runs a "!" line as a shell command and a "/" line as
    a slash command, and a control or format character can move "!" to the front once typed."""
    if any(unicodedata.category(c).startswith("C") and not c.isspace() for c in reply):
        return "the reply holds a control character"
    line = " ".join(reply.split())
    if line.startswith("!"):
        return "a reply that starts with ! runs a shell command; type it in the pane"
    if line.startswith("/"):
        return "a reply that starts with / runs a Claude command; type it in the pane"
    return None


def _tap(reply: str) -> bool:
    return _unsafe(reply) is None and not re.search(r"<[^>]+>", reply)


def _ask(n: int, lines: dict, options: list) -> dict | None:
    if not lines.get("ask") or "reply" not in lines:
        return None
    replies = LITERAL.findall(lines["reply"])
    if not replies or [o["n"] for o in options] != list(range(1, len(options) + 1)):
        return None
    prose = set(re.sub(r"\bor\b", "", LITERAL.sub("", lines["reply"]))) - set(" ,")
    return {"n": n, "ask": lines["ask"], "options": options,       # prose: a quoted word may not be a reply
            "reply_with": [{"text": r, "tap": not prose and _tap(r)} for r in replies],
            "unblocks": lines.get("unblocks", "")}


def parse(text: str) -> list[dict] | None:
    """The asks of a Needs-you section, or None when any part is off the format."""
    out: list[dict] = []
    cur: dict | None = None            # {"n", "lines": {field: text}, "options": [...], "at": field}

    def close() -> bool:
        if cur is None:
            return True
        a = _ask(cur["n"], cur["lines"], cur["options"])
        if a is None or any(x["n"] == a["n"] for x in out):
            return False
        out.append(a)
        return True

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("#", "```", "~~~")):
            return None                                      # a heading or code: past the format
        m = FIELD.match(line)
        if m:
            name, num, rest = m.groups()
            if name.startswith("Ask"):
                if not close():
                    return None
                cur = {"n": int(num) if num else len(out) + 1, "lines": {"ask": rest}, "options": [],
                       "at": "ask"}
                continue
            if cur is None:
                return None
            key = {"Options": "options", "Reply with": "reply", "Unblocks": "unblocks"}[name]
            if key in cur["lines"]:
                return None
            cur["lines"][key] = rest
            cur["at"] = key
            continue
        if cur is None:
            return None
        o = OPTION.match(line)
        if o and cur["at"] in ("options", "option"):
            cur["options"].append({"n": int(o.group(1)), "text": o.group(2)})
            cur["at"] = "option"
        elif line.startswith("**"):
            return None                                      # a field or section this format does not know
        elif cur["at"] == "option":
            cur["options"][-1]["text"] += " " + line
        else:
            cur["lines"][cur["at"]] = (cur["lines"][cur["at"]] + " " + line).strip()
    if cur is None or not close():
        return None
    if len(out) > 1:                                         # one line cannot say which ask it answers
        seen = [r["text"] for a in out for r in a["reply_with"]]
        for a in out:
            for r in a["reply_with"]:
                if seen.count(r["text"]) > 1:
                    r["tap"] = False
    return out


def _read(f: Path) -> dict | None:
    try:
        item = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    return item if isinstance(item, dict) and isinstance(item.get("pane"), str) else None


def cards(herdr, queue: Path, cache: Path, open_asks: list[dict], now: float) -> list[dict]:
    """One card per live pane's item, oldest first."""
    live = {p.pane_id: p for p in herdr.panes()}
    try:
        spaces = {w.workspace_id: w.label for w in herdr.workspaces()}
    except Exception:
        spaces = {}
    ids = {a.get("id") for a in open_asks if isinstance(a, dict)}
    out = []
    for name, it in sidebar._items(queue):
        pane = live.get(it["pane"])
        if name.startswith("hx_") or pane is None:
            continue
        text = it["ask"] if isinstance(it.get("ask"), str) else ""
        ts = it.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
            ts = None
        left, working, _ = sidebar.cache_left(pane.pane_id, cache, now)
        out.append({"pane": pane.pane_id, "workspace": pane.workspace_id,
                    "space": spaces.get(pane.workspace_id, ""), "session": pane.session,
                    "ts": ts, "item": name, "sha": sha(text),
                    "cache": {"left_s": None if left is None else round(left),
                              "text": sidebar.cache_text(left, working)},
                    "trk_ask": sidebar.match(name, it, ids), "asks": parse(text), "raw": text})
    return sorted(out, key=lambda c: (c["ts"] or 0, c["pane"]))


def _changed(queue: Path, pane: str, digest: str) -> str | None:
    it = _read(queue / f"{pane.replace(':', '_')}.json")
    if it is None:
        return "the ask is gone (answered, resolved or dismissed)"
    if sha(str(it.get("ask", ""))) != digest:
        return "the ask changed since the card was drawn: reload"
    return None


def _answered(folder: Path, pane: str, digest: str, ts, now: float) -> Path:
    """The marker of one answered ask: its pane, its text (sha) and when it was asked (the item's
    ts), so the same words asked again later are a new ask."""
    for old in folder.glob("*.done"):
        try:
            if now - old.stat().st_mtime > ANSWERED_KEEP_S:
                old.unlink()
        except OSError:
            pass
    asked = re.sub(r"[^0-9A-Za-z.-]", "_", str(ts))[:40]
    return folder / f"{pane.replace(':', '_')}-{digest}-{asked}.done"


def answer(herdr, queue: Path, pane: str, digest: str, session: str, text: str, wait: float,
           clock=time.monotonic, sleep=time.sleep) -> str:
    """"sent" or "refused: <why>". An empty `session` stands for a pane herdr names no session for."""
    if not text.strip():
        return "refused: the reply is empty"
    why = _unsafe(text)
    if why:
        return f"refused: {why}"
    expected = session or None
    folder = state_dir() / "answered"
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{pane.replace(':', '_')}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)                     # a second tap waits for the first
        why = _changed(queue, pane, digest)
        if why:
            return f"refused: {why}"
        it = _read(queue / f"{pane.replace(':', '_')}.json") or {}
        done = _answered(folder, pane, digest, it.get("ts"), time.time())
        if done.exists():
            return "refused: this ask was answered already"

        def still(p: str, now_session) -> str | None:
            if now_session != expected:
                return "the pane runs another session now"
            return _changed(queue, p, digest)

        if {p.pane_id: p.session for p in herdr.panes()}.get(pane, expected) != expected:
            return "refused: the pane runs another session now"
        result = send(herdr, [pane], text, wait, clock=clock, sleep=sleep, still=still)[pane]
        if result == "sent":
            done.touch()
        return result
