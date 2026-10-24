"""Thin adapter over the herdr CLI. Panes are matched by Claude session, never by pane id."""
import json
import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path


TOKEN_TTL_MS = 30 * 60 * 1000   # longer than the 10-minute re-report, so a live watcher never lets one lapse


class HerdrError(Exception):
    pass


@dataclass(frozen=True)
class Pane:
    pane_id: str
    workspace_id: str
    session: str | None
    status: str
    cwd: str
    tab_id: str = ""
    fg_cwd: str = ""       # the foreground process's folder: Claude's, inside a worktree

    @property
    def wakeable(self) -> bool:
        return self.status in ("idle", "done")


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    label: str


@dataclass(frozen=True)
class Created:
    workspace_id: str
    tab_id: str
    pane_id: str


def parse_panes(reply: dict) -> list[Pane]:
    return [Pane(p["pane_id"], p["workspace_id"], (p.get("agent_session") or {}).get("value"),
                 p.get("agent_status", "unknown"), p.get("cwd", ""), p.get("tab_id", ""),
                 p.get("foreground_cwd", ""))
            for p in reply["result"]["panes"]]


def parse_workspaces(reply: dict) -> list[Workspace]:
    return [Workspace(w["workspace_id"], w.get("label", "")) for w in reply["result"]["workspaces"]]


def parse_created(reply: dict) -> Created:
    p = reply["result"]["root_pane"]
    return Created(p["workspace_id"], p["tab_id"], p["pane_id"])


class Herdr:
    def __init__(self, bin: str = "herdr"):
        self.bin = bin
        # the default session's API socket, which `--session default` also reaches
        self.socket = os.environ.get("HX_HERDR_SOCKET") or str(Path.home() / ".config/herdr/herdr.sock")

    def _request(self, method: str, params: dict) -> dict:
        """One socket request (the CLI has no command for some methods, such as agent.view.set)."""
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(10)
                s.connect(self.socket)
                s.sendall((json.dumps({"id": "hx", "method": method, "params": params}) + "\n").encode())
                line = s.makefile().readline()
            reply = json.loads(line)
        except (OSError, ValueError) as e:
            raise HerdrError(f"herdr {method}: {e}") from e
        if not isinstance(reply, dict) or "error" in reply or "result" not in reply:
            err = reply.get("error") if isinstance(reply, dict) else reply
            raise HerdrError(f"herdr {method}: {str(err)[:200]}")
        return reply["result"]

    def _run(self, *args: str) -> str:
        try:
            # The default server, whichever server the caller's shell belongs to: ids are per server.
            out = subprocess.run([self.bin, "--session", "default", *args], capture_output=True,
                                 text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise HerdrError(str(e)) from e
        if out.returncode != 0:
            raise HerdrError(f"herdr {' '.join(args[:2])}: {out.stderr.strip()[:200]}")
        return out.stdout

    def panes(self) -> list[Pane]:
        return parse_panes(json.loads(self._run("pane", "list")))

    def wake(self, pane_id: str, text: str) -> None:
        self._run("pane", "run", pane_id, text)

    def read(self, pane_id: str) -> str:
        """The visible screen with colours: drafts.py tells a typed draft from a dim placeholder."""
        return self._run("pane", "read", pane_id, "--source", "visible", "--ansi")

    def workspaces(self) -> list[Workspace]:
        return parse_workspaces(json.loads(self._run("workspace", "list")))

    def create_space(self, label: str, cwd: str, env: dict[str, str]) -> Created:
        args = ["workspace", "create", "--no-focus", "--label", label, "--cwd", cwd]
        for k, v in env.items():
            args += ["--env", f"{k}={v}"]
        return parse_created(json.loads(self._run(*args)))

    def rename_tab(self, tab_id: str, label: str) -> None:
        self._run("tab", "rename", tab_id, label)

    def run(self, pane_id: str, command: str) -> None:
        self._run("pane", "run", pane_id, command)

    def report_space(self, workspace_id: str, value: str | None) -> None:
        self._report("workspace", workspace_id, "hx", value)

    def report_pane(self, pane_id: str, value: str | None) -> None:
        self._report("pane", pane_id, "hx_sub", value)

    def _report(self, what: str, target: str, token: str, value: str | None) -> None:
        tok = ["--token", f"{token}={value}"] if value is not None else ["--clear-token", token]
        self._run(what, "report-metadata", target, "--source", "hx", "--ttl-ms", str(TOKEN_TTL_MS), *tok)

    def close_space(self, workspace_id: str) -> None:
        self._run("workspace", "close", workspace_id)

    def report_tokens(self, pane_id: str, tokens: dict[str, str], ttl_ms: int) -> None:
        args = [a for k, v in tokens.items() for a in ("--token", f"{k}={v}")]
        self._run("pane", "report-metadata", pane_id, "--source", "hx", "--ttl-ms", str(ttl_ms), *args)

    def clear_tokens(self, pane_id: str, names) -> None:
        args = [a for n in names for a in ("--clear-token", n)]
        self._run("pane", "report-metadata", pane_id, "--source", "hx", *args)

    def view_set(self, view: dict) -> None:
        self._request("agent.view.set", view)
