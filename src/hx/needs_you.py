"""herdr-jump's needs-you queue holds one item per pane whose agent ended a turn with a
"## ⚑ Needs you" section. hx clears that item when the pane's ask is answered and the item names
that ask: an item about anything else, or a newer one, stays. The queue's own prompt hook keeps
the item, because the wake's "[hx]" line reads as a machine's, not a person's answer.
"""
import json
import os
import re
import subprocess
from pathlib import Path

PLUGIN = "herdr-jump@mjfork"
QUEUE = Path.home() / ".herdr-queue" / "default"     # herdr.py always talks to the default server


class NeedsYouError(Exception):
    pass


def script() -> Path | None:
    """needs-you.sh of the installed herdr-jump plugin; its folder changes with each version."""
    registry = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "plugins/installed_plugins.json"
    try:
        path = Path(json.loads(registry.read_text())["plugins"][PLUGIN][0]["installPath"]) / "bin/needs-you.sh"
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None
    return path if path.is_file() else None


def clear(pane: str, ask: str, queue: Path | None = None) -> None:
    """Drop the pane's needs-you item if it names `ask`. Without herdr-jump, nothing to do."""
    s = script()
    if s is None:
        return
    try:
        item = json.loads(((queue or QUEUE) / f"{pane.replace(':', '_')}.json").read_text())
    except (OSError, ValueError):
        return                                 # no item (or none hx can read): nothing to clear
    if not isinstance(item, dict) or not re.search(rf"(?<![\w-]){re.escape(ask)}(?![\w-])", str(item.get("ask", ""))):
        return
    try:
        # herdr.py always talks to the default server; pane ids are per server, so the queue must match
        out = subprocess.run(["bash", str(s), "clear", pane, "answered"], capture_output=True, text=True,
                             timeout=10, env={**os.environ, "HERDR_SESSION": "default"})
    except subprocess.TimeoutExpired:
        raise NeedsYouError("timed out after 10 seconds") from None
    if out.returncode != 0:
        raise NeedsYouError(f"exit {out.returncode}: {(out.stderr or out.stdout).strip()[:200]}")
