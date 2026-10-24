"""Human channels: console (inbox.log, shown by the inbox pane) and the needs-you queue."""
import json
import os
import time
from pathlib import Path

from hx.routing import Clear, Notify


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Channels:
    def __init__(self, inbox: Path, queue_dir: Path, workspaces: dict[str, str]):
        self.inbox, self.queue_dir, self.workspaces = inbox, queue_dir, workspaces

    def _log(self, line: str) -> None:
        self.inbox.parent.mkdir(parents=True, exist_ok=True)
        with self.inbox.open("a") as f:
            f.write(f"{_now()}  {line}\n")

    def log(self, line: str) -> None:
        self._log(line)

    def notify(self, n: Notify) -> bool:
        if n.channel == "console":
            self._log(f"ASK  {n.ask}  {n.person}  {n.text}")
            return True
        if n.channel == "queue":
            if n.pane is None:
                return False
            if (self.queue_dir / f"hx_{n.ask}.json").exists():
                return True
            self.queue_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            item = {"pane": n.pane, "workspace": self.workspaces.get(n.pane, ""),
                    "ask": f"**Ask:** {n.text}\n**Reply with:** `trk answer {n.ask} "
                           f"--outcome \"<one line>\" --answer -`",
                    "cwd": str(Path.home()), "ts": int(time.time())}
            tmp = self.queue_dir / f".hx_{n.ask}.tmp"
            tmp.write_text(json.dumps(item, indent=2))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.queue_dir / f"hx_{n.ask}.json")
            return True
        raise ValueError(f"unknown channel {n.channel}")

    def sweep(self, open_asks: set[str]) -> None:
        """Drop queue items of asks that are no longer open (closed, or erased by a restore)."""
        for f in self.queue_dir.glob("hx_*.json"):
            if f.name[3:-5] not in open_asks:
                f.unlink(missing_ok=True)

    def clear(self, c: Clear) -> None:
        (self.queue_dir / f"hx_{c.ask}.json").unlink(missing_ok=True)
        self._log(f"CLEARED  {c.ask}")
