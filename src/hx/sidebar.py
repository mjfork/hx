"""herdr's Agents panel as the needs-you list. Each pass hx puts tokens on every live pane with a
queued needs-you item (the trk ask, cache time left, score) and sets an agent view that shows
only those panes, most urgent first, under the heading "needs you".

The score is trk's urgency base for the ask (tr-qwwsc: held-up items × urgent) times a cache
bump hx works out from cache-timer's real time left: 1 + 9 × (share of the cache used)², and 1
once the cache is cold. A row with no trk ask, or no urgency yet, is unscored and sorts last.

Tokens live TTL_MS, so a stopped watcher empties the panel within two minutes.
"""
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

TTL_MS = 120_000   # 4 quiet passes (hx watch --tick 30): one slow call never empties the panel
NAMES = ("hx_item", "hx_cache", "hx_score", "hx_sort", "hx_tie")
VIEW = {"source": "plugin:hx", "label": "needs you",
        "filter": {"op": "exists", "field": {"token": "hx_sort"}},
        "sort": [{"field": {"token": "hx_sort"}, "order": "desc"},     # highest score first
                 {"field": {"token": "hx_tie"}, "order": "asc"}]}      # then the cache that ends soonest
ASK_ID = re.compile(r"(?<![\w-])tr-[0-9a-z]+(?![\w-])")
NO_TIE = 99999


@dataclass(frozen=True)
class Row:
    pane: str
    ask: str | None
    cache: str             # cache_text()
    score: float | None
    left: float | None     # seconds of cache left; None when unknown or the session is working


def cache_dir() -> Path:
    """cache-timer's per-pane files, the ones cache-state.sh reads."""
    if "CACHE_STATE_DIR" in os.environ:
        return Path(os.environ["CACHE_STATE_DIR"])
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "ctx/cache"


def cache_left(pane: str, folder: Path, now: float) -> tuple[float | None, bool, int]:
    """(seconds left or None, working, ttl in ms) from cache-timer's file for the pane."""
    try:
        d = json.loads((folder / f"{pane}.json").read_text())
        expires, ttl = float(d["expiresAt"]), int(d.get("ttlMs") or 3_600_000)
        if not math.isfinite(expires) or ttl <= 0:
            raise ValueError
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None, False, 3_600_000
    return expires / 1000 - now, bool(d.get("isWorking")), ttl


def cache_text(left: float | None, working: bool) -> str:
    if working:
        return "busy"
    if left is None:
        return "?"
    if left <= 0:
        return "cold"
    m = math.ceil(left / 60)
    return f"{m}m!" if left <= 9 * 60 else f"{m}m"


def factor(left: float | None, ttl_ms: int) -> float:
    if left is None or left <= 0:
        return 1.0
    used = min(1.0, max(0.0, 1 - left / (ttl_ms / 1000)))
    return 1 + 9 * used ** 2


def _base(ask: dict | None) -> float | None:
    u = (ask or {}).get("urgency")
    b = u.get("base") if isinstance(u, dict) else None
    if isinstance(b, bool) or not isinstance(b, (int, float)) or not math.isfinite(b) or b < 0:
        return None
    return float(b)


def _items(queue: Path):
    try:
        files = sorted(queue.glob("*.json"))
    except OSError:
        return
    for f in files:
        try:
            item = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(item, dict) and isinstance(item.get("pane"), str):
            yield f.name, item


def match(name: str, item: dict, open_ids) -> str | None:
    """The open trk ask a queue item stands for: an hx_<ask>.json item by its file name, a pane's
    item by the first open ask id its text names on its own."""
    if name.startswith("hx_"):
        return name[3:-5] if name[3:-5] in open_ids else None
    return next((m for m in ASK_ID.findall(str(item.get("ask", ""))) if m in open_ids), None)


def rows(queue: Path, cache: Path, open_asks: list[dict], live: set[str], now: float) -> dict[str, Row]:
    """One row per live pane with a queued item; of two items on a pane, the higher score wins."""
    asks = {a["id"]: a for a in open_asks}
    out: dict[str, Row] = {}
    for name, item in _items(queue):
        pane = item["pane"]
        if pane not in live:
            continue
        ask = match(name, item, asks)
        left, working, ttl = cache_left(pane, cache, now)
        text = cache_text(left, working)
        if working:
            left = None                        # its cache renews when the turn ends: no bump
        base = _base(asks.get(ask)) if ask else None
        score = None if base is None else base * factor(left, ttl)
        row = Row(pane, ask, text, score, left)
        old = out.get(pane)
        if old is None or (row.score or -1) > (old.score or -1):
            out[pane] = row
    return out


def tokens(row: Row) -> dict[str, str]:
    sort = 0 if row.score is None else max(1, min(999_999, round(row.score * 10)))
    tie = NO_TIE if row.left is None or row.left <= 0 else min(NO_TIE - 1, int(row.left))
    return {"hx_item": row.ask or "⚑", "hx_cache": row.cache,
            "hx_score": "–" if row.score is None else f"▲ {row.score:.1f}",
            "hx_sort": f"{sort:06d}", "hx_tie": f"{tie:05d}"}


def publish(herdr, shown: dict[str, Row], before: list[str], live: set[str]) -> tuple[list[str], list[str]]:
    """The view first (herdr forgets it on restart, so it is sent every pass), then tokens for this
    pass's rows, then none left on live panes that dropped out. A closed pane is not cleared: its
    tokens went with it. One failing call never stops the rest. Returns (panes now shown, errors)."""
    errors: list[str] = []
    try:
        herdr.view_set(VIEW)
    except Exception as e:
        errors.append(f"view: {type(e).__name__}: {e}")
    done: list[str] = []
    for pane, row in shown.items():
        try:
            herdr.report_tokens(pane, tokens(row), TTL_MS)
            done.append(pane)
        except Exception as e:
            errors.append(f"{pane}: {type(e).__name__}: {e}")
    for pane in before:
        if pane not in shown and pane in live:
            try:
                herdr.clear_tokens(pane, NAMES)
            except Exception as e:
                errors.append(f"{pane}: {type(e).__name__}: {e}")
    return done, errors

STALE_S = 120     # the watcher passes every 15 s or so; two minutes of silence means it stopped


def write_line(path: Path, shown: dict[str, Row], now: float) -> None:
    """What the tab bar shows, for `hx needs-you --line` (herdr runs it every 15 s)."""
    scored = [r for r in shown.values() if r.score is not None]
    top = max(scored, key=lambda r: r.score, default=None)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps({"at": now, "count": len(shown),
                               "top": f"{top.ask} {top.score:.0f}" if top else None}))
    os.replace(tmp, path)


def line(path: Path, now: float) -> str:
    try:
        d = json.loads(path.read_text())
        at, count, top = float(d["at"]), int(d["count"]), d.get("top")
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return "hx stale"
    if not math.isfinite(at) or now - at > STALE_S:
        return "hx stale"
    if count == 0:
        return ""
    return f"⚑ {count}" + (f" · {top}" if top else "")
