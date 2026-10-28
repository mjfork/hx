import json
import os
import re
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def trk_home(tmp_path, monkeypatch):
    """A throwaway trk store: never the live one."""
    home = tmp_path / "trk"
    home.mkdir()
    monkeypatch.setenv("TRK_HOME", str(home))
    run(home, "init", "--me", "michael")
    # newer trk registers the worker agent at init
    have = {a["id"] for a in run(home, "actors")["actors"]}
    for actor in ("worker", "orchestrator"):
        if actor not in have:
            run(home, "actor", "add", actor, "--kind", "agent")
    return home


def run(home: Path, *args: str, stdin: str | None = None) -> dict:
    # TRK_ACTOR: trk refuses an agent session's write without an actor; --as still wins
    env = {**os.environ, "TRK_HOME": str(home), "TRK_ACTOR": "michael"}
    out = subprocess.run(["trk", *args, "--json"], input=stdin, capture_output=True,
                         text=True, env=env)
    data = json.loads(out.stdout or "{}")
    if out.returncode != 0:
        raise AssertionError(f"trk {' '.join(args)} failed: {data}")
    return data


@pytest.fixture
def sh(trk_home):
    """sh("claim", id, "--as", "worker", "--session", "s1") drives the throwaway store."""
    return lambda *a, stdin=None: run(trk_home, *a, stdin=stdin)


@pytest.fixture
def tree(sh, tmp_path):
    """A program with one self-contained ticket, claimed by worker session s-worker."""
    (tmp_path / "cb.md").write_text("now\n")
    (tmp_path / "db.md").write_text("want\n")
    prog = sh("new", "program", "Prog", "--criterion", "c1")["item"]["id"]
    tick = sh("new", "work-item", "Tick", "--form", "self-contained", "--parent", prog,
              "--section", f"current-behaviour=@{tmp_path / 'cb.md'}",
              "--section", f"desired-behaviour=@{tmp_path / 'db.md'}",
              "--criterion", "c1")["item"]["id"]
    sh("claim", tick, "--as", "worker", "--session", "s-worker")
    return {"program": prog, "ticket": tick}


from hx.herdr import Created, Pane, Workspace  # noqa: E402

RULE = "─" * 40


def box(*lines: str) -> str:
    """A Claude screen as `herdr pane read --ansi` gives it: history, then the input box, then status."""
    rule = f"\x1b[0m\x1b[38;2;136;136;136m{RULE}\x1b[0m"
    first, *rest = lines or ("",)
    body = [f"❯\u00a0\x1b[0m{first}\x1b[0m", *rest]
    return "\r\n".join(["Brewed for 6m", rule, *body, rule, "  ~/projects/app [app] Opus", ""])


EMPTY_BOX = box()


@pytest.fixture(autouse=True)
def no_live_dirs(tmp_path_factory, monkeypatch):
    """hx's state and config resolve to throwaway paths: a test never reads, writes or moves the
    live ~/.local/state/hx or ~/.config/hx, nor their old herdr-extension names."""
    d = tmp_path_factory.mktemp("hx-dirs")
    monkeypatch.setenv("HX_STATE_DIR", str(d / "state"))
    monkeypatch.setenv("HX_CONFIG", str(d / "config.toml"))
    monkeypatch.setenv("CACHE_STATE_DIR", str(d / "cache"))        # cache-timer's files (sidebar.py)
    monkeypatch.setenv("HX_HERDR_SOCKET", str(d / "herdr.sock"))   # never the live herdr socket


@pytest.fixture(autouse=True)
def no_live_herdr(monkeypatch):
    """A test that reaches the real herdr client fails instead of reading or typing into a live pane."""
    import hx.herdr

    real = hx.herdr.Herdr._run

    import shutil
    live = shutil.which("herdr")

    def guarded(self, *args):
        found = shutil.which(str(self.bin))
        if str(self.bin) == "herdr" or (live and found and Path(found).resolve() == Path(live).resolve()):
            # the real one, by name or by path; tests may run a fake binary by its own path
            raise hx.herdr.HerdrError(f"tests never call live herdr (herdr {' '.join(args[:2])})")
        return real(self, *args)
    monkeypatch.setattr(hx.herdr.Herdr, "_run", guarded)

    real_request = hx.herdr.Herdr._request
    live_sock = (Path.home() / ".config/herdr/herdr.sock").resolve()

    def guarded_request(self, method, params):
        if Path(self.socket).resolve() == live_sock:
            raise hx.herdr.HerdrError(f"tests never call live herdr ({method})")
        return real_request(self, method, params)
    monkeypatch.setattr(hx.herdr.Herdr, "_request", guarded_request)


@pytest.fixture(autouse=True)
def cleared(monkeypatch):
    """needs-you.sh never runs in a test (it would drop the live queue's items): the panes hx
    would clear are recorded here instead."""
    import hx.needs_you
    panes: list[str] = []
    monkeypatch.setattr(hx.needs_you, "clear", lambda pane, ask: panes.append(pane))
    return panes


@pytest.fixture(autouse=True)
def no_quiet_period(monkeypatch):
    """Tests wake on the first pass; the 30-second quiet period has its own tests."""
    import hx.drafts
    monkeypatch.setattr(hx.drafts, "QUIET_S", 0)


WAKE_HEADER = re.compile(r"^\[[a-z]+\] id:\S+ from:hx ")
TYPED: list[list] = []      # every FakeHerdr's typed lines, for the header check below


@pytest.fixture(autouse=True)
def every_wake_has_its_header(request):
    """Upstream ask UP-0003: every line hx types into a pane opens with `[<kind>] id:<id> from:hx `, nothing before
    it. Checked on every test's typed lines, except where hx types a person's own answer (hx ask answer)."""
    TYPED.clear()
    yield
    if request.module.__name__.rsplit(".", 1)[-1] in ("test_asks", "test_send"):
        return
    bad = [line for typed in TYPED for _, line in typed if not WAKE_HEADER.match(line)]
    assert not bad, f"typed without the wake header: {bad}"


class FakeHerdr:
    """herdr without herdr: returns the given snapshot and records every write in `calls`."""

    def __init__(self, panes: list[Pane], workspaces: list[Workspace] | None = None,
                 screens: dict | None = None):
        self._panes = panes
        self._screens = screens or {}       # pane -> screen text, or an exception to raise
        self.reads: list[str] = []
        self._workspaces = workspaces if workspaces is not None else \
            [Workspace(w, "") for w in sorted({p.workspace_id for p in panes})]
        self.wakes: list[tuple[str, str]] = []
        self.typed: list[tuple[str, str]] = []
        TYPED.append(self.typed)
        self.calls: list[tuple] = []
        self.sidebar: list[tuple] = []      # the needs-you panel's tokens and view (sidebar.py)

    def panes(self) -> list[Pane]:
        return self._panes

    def workspaces(self) -> list[Workspace]:
        return self._workspaces

    def wake(self, pane_id: str, text: str) -> None:
        # `typed` is the line as typed; `wakes` drops the automation header (upstream ask UP-0003), which
        # test_wake_header checks on every kind, so the other tests read the text alone
        self.typed.append((pane_id, text))
        self.wakes.append((pane_id, WAKE_HEADER.sub("", text, count=1)))

    def read(self, pane_id: str) -> str:
        self.reads.append(pane_id)
        screen = self._screens.get(pane_id, EMPTY_BOX)
        if isinstance(screen, Exception):
            raise screen
        return screen

    def create_space(self, label: str, cwd: str, env: dict) -> Created:
        self.calls.append(("create_space", label, cwd, env))
        return Created("wNEW", "wNEW:t1", "wNEW:p1")

    def rename_tab(self, tab_id: str, label: str) -> None:
        self.calls.append(("rename_tab", tab_id, label))

    def run(self, pane_id: str, command: str) -> None:
        self.calls.append(("run", pane_id, command))

    def report_space(self, workspace_id: str, value) -> None:
        self.calls.append(("report_space", workspace_id, value))

    def report_pane(self, pane_id: str, value) -> None:
        self.calls.append(("report_pane", pane_id, value))

    def close_space(self, workspace_id: str) -> None:
        self.calls.append(("close_space", workspace_id))

    def report_tokens(self, pane_id: str, tokens: dict, ttl_ms: int) -> None:
        self.sidebar.append(("report_tokens", pane_id, tokens, ttl_ms))

    def clear_tokens(self, pane_id: str, names) -> None:
        self.sidebar.append(("clear_tokens", pane_id, tuple(names)))

    def view_set(self, view: dict) -> None:
        self.sidebar.append(("view_set", view))
