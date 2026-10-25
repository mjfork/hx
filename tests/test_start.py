import json
import subprocess
from pathlib import Path

import pytest

from hx.config import load_config
from hx.herdr import HerdrError, Workspace
from hx.start import StartError, start
from hx.tracker import Trk
from tests.conftest import FakeHerdr

SONNET = "claude-sonnet-5-5"


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    return str(r)


class Recorder:
    def __init__(self, held=None):
        self.held = held
        self.guards: list[tuple[str, bool]] = []
        self.spawns: list[tuple] = []

    def guard(self, label, check_only):
        self.guards.append((label, check_only))
        return self.held

    def spawn(self, worktree, log):
        self.spawns.append((worktree, str(log)))


def go(trk_home, tmp_path, h, root, rec=None, **kw):
    rec = rec or Recorder()
    return start(Trk(home=str(trk_home)), h, tmp_path / "state.json", load_config(tmp_path / "none"), root,
                 guard=rec.guard, spawn=rec.spawn, **kw)


def binding(tmp_path, root):
    p = tmp_path / "state.json"
    return json.loads(p.read_text())["bindings"].get(root) if p.exists() else None


def solo_root(sh, repo, *extra):
    return ticket_root(sh, repo, *extra)


def test_childless_root_starts_solo_through_claude_worker(trk_home, sh, tmp_path, repo, monkeypatch):
    monkeypatch.setattr("hx.config.shutil.which", lambda name: f"/opt/bin/{name}")   # installed here
    r = solo_root(sh, repo)
    h, rec = FakeHerdr([]), Recorder()
    go(trk_home, tmp_path, h, r, rec)
    assert [c[0] for c in h.calls] == ["create_space", "rename_tab", "run"]
    assert h.calls[0][1] == f"{r} Tick" and h.calls[0][2] == repo
    assert h.calls[2] == ("run", "wNEW:p1", f"claude-worker --model '{SONNET}' --effort medium -w {r} '/hx-work {r}'")
    assert rec.guards == [(r, False)]
    b = binding(tmp_path, r)
    assert b["origin"] == "hx" and b["role"] == "solo" and b["pane"] == "wNEW:p1"
    assert b["launch"] == {"model": SONNET, "effort": "medium", "reason": None, "parent": None}


def test_space_env_carries_role_and_root(trk_home, sh, tmp_path, repo):
    r = solo_root(sh, repo)
    h = FakeHerdr([])
    go(trk_home, tmp_path, h, r)
    assert h.calls[0][3] == {"HX_ROLE": "solo", "HX_ROOT": r}


def test_a_guard_override_reaches_the_pane(trk_home, sh, tmp_path, repo, monkeypatch):
    """The override that let hx start past the guard must reach claude-worker's guard in the pane."""
    r = solo_root(sh, repo)
    monkeypatch.setenv("HERDR_HOLD_OVERRIDE", "1")
    monkeypatch.setenv("DISPATCH_FORCE", "1")
    h = FakeHerdr([])
    go(trk_home, tmp_path, h, r)
    assert h.calls[0][3] == {"HX_ROLE": "solo", "HX_ROOT": r, "HERDR_HOLD_OVERRIDE": "1", "DISPATCH_FORCE": "1"}


def test_worktree_is_the_jira_key(trk_home, sh, tmp_path, repo):
    r = solo_root(sh, repo, "--jira", "https://x.atlassian.net/browse/PROJ-42")
    h, rec = FakeHerdr([]), Recorder()
    go(trk_home, tmp_path, h, r, rec)
    assert " -w proj-42 " in h.calls[2][2]
    assert rec.spawns[0][0] == f"{repo}/.claude/worktrees/proj-42"
    assert rec.spawns[0][1].endswith("/deps/proj-42.log")


def test_guard_holds_and_nothing_is_created(trk_home, sh, tmp_path, repo):
    r = solo_root(sh, repo)
    h = FakeHerdr([])
    with pytest.raises(StartError, match="held: load 40 above 32"):
        go(trk_home, tmp_path, h, r, Recorder(held="load 40 above 32"))
    assert h.calls == [] and binding(tmp_path, r) is None


def test_root_with_children_is_refused_until_3b(trk_home, tree, tmp_path):
    with pytest.raises(StartError, match="slice 3b"):
        go(trk_home, tmp_path, FakeHerdr([]), tree["program"])


def test_refuses_a_non_root(trk_home, tree, tmp_path):
    with pytest.raises(StartError, match="not a root"):
        go(trk_home, tmp_path, FakeHerdr([]), tree["ticket"])


def test_root_without_a_checkout_is_refused(trk_home, sh, tmp_path):
    (tmp_path / "cb.md").write_text("now\n")
    (tmp_path / "db.md").write_text("want\n")
    r = sh("new", "work-item", "No repo", "--form", "self-contained", "--criterion", "c1",
           "--section", f"current-behaviour=@{tmp_path / 'cb.md'}",
           "--section", f"desired-behaviour=@{tmp_path / 'db.md'}")["item"]["id"]
    with pytest.raises(StartError, match="--add-repo"):
        go(trk_home, tmp_path, FakeHerdr([]), r)


@pytest.mark.parametrize("model", ["claude-opus-5-5", "Claude-Opus-5-5"])
def test_opus_needs_a_reason(trk_home, sh, tmp_path, repo, model):
    r = solo_root(sh, repo)
    with pytest.raises(StartError, match="reason"):
        go(trk_home, tmp_path, FakeHerdr([]), r, model=model)
    go(trk_home, tmp_path, FakeHerdr([]), r, model=model, reason="kickoff framing")
    launch = binding(tmp_path, r)["launch"]
    assert launch["model"] == model and launch["reason"] == "kickoff framing"


@pytest.mark.parametrize("kw", [{"effort": "huge"}, {"model": "x;rm"}])
def test_bad_effort_or_model_is_refused_before_anything(trk_home, sh, tmp_path, repo, kw):
    r = solo_root(sh, repo)
    h, rec = FakeHerdr([]), Recorder()
    with pytest.raises(StartError):
        go(trk_home, tmp_path, h, r, rec, **kw)
    assert h.calls == [] and rec.guards == []


def test_dry_run_checks_but_creates_nothing(trk_home, sh, tmp_path, repo):
    r = solo_root(sh, repo)
    h, rec = FakeHerdr([]), Recorder()
    out = go(trk_home, tmp_path, h, r, rec, dry_run=True)
    assert f"'/hx-work {r}'" in out and "guard ok" in out
    assert rec.guards == [(r, True)] and rec.spawns == [] and h.calls == []
    assert binding(tmp_path, r) is None


def test_parent_and_effort_are_recorded(trk_home, sh, tmp_path, repo):
    r = solo_root(sh, repo)
    h = FakeHerdr([])
    go(trk_home, tmp_path, h, r, effort="high", parent="s-boss")
    assert "--effort high" in h.calls[2][2]
    assert binding(tmp_path, r)["launch"]["parent"] == "s-boss"


def test_deps_skipped_with_DISPATCH_NO_DEPS(trk_home, sh, tmp_path, repo, monkeypatch):
    monkeypatch.setenv("DISPATCH_NO_DEPS", "1")
    r = solo_root(sh, repo)
    rec = Recorder()
    go(trk_home, tmp_path, FakeHerdr([]), r, rec)
    assert rec.spawns == []


class RunFails(FakeHerdr):
    def run(self, pane_id, command):
        raise HerdrError("pane run refused")


def test_herdr_failure_closes_the_half_made_space(trk_home, sh, tmp_path, repo):
    r = solo_root(sh, repo)
    h = RunFails([])
    with pytest.raises(StartError, match="pane run refused"):
        go(trk_home, tmp_path, h, r)
    assert ("close_space", "wNEW") in h.calls and binding(tmp_path, r) is None


def test_second_start_does_not_open_a_duplicate(trk_home, sh, tmp_path, repo):
    r = solo_root(sh, repo)
    go(trk_home, tmp_path, FakeHerdr([]), r)
    h, rec = FakeHerdr([], workspaces=[Workspace("wNEW", "")]), Recorder()
    assert "already open" in go(trk_home, tmp_path, h, r, rec) and h.calls == [] and rec.guards == []


def test_run_guard_allows_when_no_guard_is_installed(monkeypatch):
    from hx import start as s
    monkeypatch.setenv("PATH", "/nonexistent")
    assert s.run_guard("tr-x", True) is None


def test_spawn_deps_does_nothing_without_ticket_deps(monkeypatch, tmp_path):
    from hx import start as s
    monkeypatch.setenv("PATH", "/nonexistent")
    s.spawn_deps(str(tmp_path / "wt"), tmp_path / "deps" / "wt.log")
    assert not (tmp_path / "deps").exists()


# Final review fixes.
def ticket_root(sh, repo, *extra, form="self-contained"):
    d = Path(repo).parent
    (d / "cb.md").write_text("now\n")
    (d / "db.md").write_text("want\n")
    return sh("new", "work-item", "Tick", "--form", form, "--repo", repo,
              "--section", f"current-behaviour=@{d / 'cb.md'}", "--section", f"desired-behaviour=@{d / 'db.md'}",
              "--criterion", "c1", *extra)["item"]["id"]


@pytest.mark.parametrize("url,wt", [
    ("https://example.atlassian.net/browse/PROJ-42?atlOrigin=eyJp&cloudId=x", "proj-42"),
    ("https://x.atlassian.net/browse/PROJ-42?atlOrigin=x&cloudId=5f3e-1234", "proj-42"),
    ("https://x.atlassian.net/browse/PROJ-42?atlOrigin=eyJpIjoiZjA4-123", "proj-42"),
    ("PROJ-7", "proj-7"),
    ("https://x.atlassian.net/jira/boards/7?selectedIssue=PROJ-43", None),
    ("https://x.atlassian.net/browse/PROJ-1 ;id", "proj-1"),
])
def test_worktree_takes_only_the_jira_key(trk_home, sh, tmp_path, repo, url, wt):
    r = ticket_root(sh, repo, "--jira", url)
    h = FakeHerdr([])
    go(trk_home, tmp_path, h, r)
    wt = wt or r
    assert f" -w {wt} " in h.calls[2][2] and ";" not in h.calls[2][2] and "&" not in h.calls[2][2]


def test_worktree_falls_back_to_the_root_when_no_key(trk_home, sh, tmp_path, repo):
    r = ticket_root(sh, repo, "--jira", "https://x/$(touch pwned)")
    h = FakeHerdr([])
    go(trk_home, tmp_path, h, r)
    assert f" -w {r} " in h.calls[2][2] and "$(" not in h.calls[2][2]


def test_a_spec_bound_root_is_refused(trk_home, sh, tmp_path, repo):
    (Path(repo).parent / "w.md").write_text("build\n")
    r = sh("new", "work-item", "Spec", "--form", "spec-bound", "--repo", repo,
           "--section", f"what-to-build=@{Path(repo).parent / 'w.md'}", "--criterion", "c1")["item"]["id"]
    with pytest.raises(StartError, match="self-contained"):
        go(trk_home, tmp_path, FakeHerdr([]), r)


def test_root_id_is_the_trk_spelling(trk_home, sh, tmp_path, repo):
    r = ticket_root(sh, repo)
    h = FakeHerdr([])
    go(trk_home, tmp_path, h, r.upper())
    assert binding(tmp_path, r) is not None and h.calls[0][3]["HX_ROOT"] == r


def test_spawn_failure_still_saves_the_binding(trk_home, sh, tmp_path, repo):
    r = ticket_root(sh, repo)
    rec = Recorder()

    def boom(worktree, log):
        raise OSError("read-only")
    rec.spawn = boom
    out = go(trk_home, tmp_path, FakeHerdr([]), r, rec)
    assert binding(tmp_path, r) is not None and "deps not started" in out


class RunAndCloseFail(FakeHerdr):
    def run(self, pane_id, command):
        raise HerdrError("pane run refused")

    def close_space(self, workspace_id):
        raise HerdrError("close refused")


def test_failed_close_is_reported_not_hidden(trk_home, sh, tmp_path, repo):
    r = ticket_root(sh, repo)
    with pytest.raises(StartError, match="could not close wNEW"):
        go(trk_home, tmp_path, RunAndCloseFail([]), r)


def test_a_project_root_is_refused(trk_home, sh, tmp_path, repo):
    r = sh("new", "project", "Proj", "--repo", repo)["item"]["id"]
    with pytest.raises(StartError, match="self-contained"):
        go(trk_home, tmp_path, FakeHerdr([]), r)


@pytest.mark.parametrize("dry", [False, True])
def test_existing_worktree_is_refused(trk_home, sh, tmp_path, repo, dry):
    r = ticket_root(sh, repo, "--jira", "https://x.atlassian.net/browse/PROJ-42")
    (Path(repo) / ".claude/worktrees/proj-42").mkdir(parents=True)
    h, rec = FakeHerdr([]), Recorder()
    with pytest.raises(StartError, match=r"\.claude/worktrees/proj-42"):
        go(trk_home, tmp_path, h, r, rec, dry_run=dry)
    assert h.calls == [] and rec.guards == []


def test_open_space_wins_over_its_own_worktree(trk_home, sh, tmp_path, repo):
    r = ticket_root(sh, repo, "--jira", "https://x.atlassian.net/browse/PROJ-42")
    go(trk_home, tmp_path, FakeHerdr([]), r)
    (Path(repo) / ".claude/worktrees/proj-42").mkdir(parents=True)
    assert "already open" in go(trk_home, tmp_path, FakeHerdr([], workspaces=[Workspace("wNEW", "")]), r)


class CreateTimesOut(FakeHerdr):
    """herdr made the workspace, but the reply never came back."""
    def create_space(self, label, cwd, env):
        self.calls.append(("create_space", label, cwd, env))
        self._workspaces = [Workspace("wLOST", label)]
        raise HerdrError("timed out after 30 seconds")


def test_create_failure_closes_a_workspace_herdr_did_make(trk_home, sh, tmp_path, repo):
    r = ticket_root(sh, repo)
    h = CreateTimesOut([], workspaces=[])
    with pytest.raises(StartError, match="timed out"):
        go(trk_home, tmp_path, h, r)
    assert ("close_space", "wLOST") in h.calls and binding(tmp_path, r) is None


@pytest.mark.parametrize("reason", ["   ", "two\nlines"])
def test_opus_reason_must_be_one_real_line(trk_home, sh, tmp_path, repo, reason):
    r = ticket_root(sh, repo)
    h = FakeHerdr([])
    with pytest.raises(StartError, match="reason"):
        go(trk_home, tmp_path, h, r, model="claude-opus-5-5", reason=reason)
    assert h.calls == []


class CreateAndListGarbled(FakeHerdr):
    def create_space(self, label, cwd, env):
        raise ValueError("bad JSON from herdr")

    def workspaces(self):
        raise ValueError("bad JSON from herdr")


def test_garbled_herdr_replies_still_give_a_start_error(trk_home, sh, tmp_path, repo):
    r = ticket_root(sh, repo)
    with pytest.raises(StartError, match="herdr could"):
        go(trk_home, tmp_path, CreateAndListGarbled([]), r)


class CreateFailsNothingMade(FakeHerdr):
    def create_space(self, label, cwd, env):
        raise HerdrError("refused")


def test_failed_create_leaves_an_existing_same_label_space_alone(trk_home, sh, tmp_path, repo):
    r = ticket_root(sh, repo)
    h = CreateFailsNothingMade([], workspaces=[Workspace("wOLD", f"{r} Tick")])
    with pytest.raises(StartError):
        go(trk_home, tmp_path, h, r)
    assert ("close_space", "wOLD") not in h.calls


def test_model_with_a_trailing_newline_is_refused(trk_home, sh, tmp_path, repo):
    r = ticket_root(sh, repo)
    h = FakeHerdr([])
    with pytest.raises(StartError, match="bad model"):
        go(trk_home, tmp_path, h, r, model="claude-sonnet-5-5\n")
    assert h.calls == []


def test_run_guard_holds_when_the_script_cannot_run(monkeypatch, tmp_path):
    from hx import start as s
    g = tmp_path / "launch-guard.sh"
    g.write_text("#!/bin/sh\nexit 0\n")      # not executable
    monkeypatch.setenv("PATH", str(tmp_path))
    assert "launch-guard.sh" in s.run_guard("tr-x", True)


def test_explicit_empty_model_is_refused(trk_home, sh, tmp_path, repo):
    r = ticket_root(sh, repo)
    h = FakeHerdr([])
    with pytest.raises(StartError, match="bad model"):
        go(trk_home, tmp_path, h, r, model="")
    assert h.calls == []


def test_run_guard_holds_when_the_guard_is_present_but_broken(monkeypatch, tmp_path):
    from hx import start as s
    g = tmp_path / "launch-guard.sh"
    g.write_text("#!/nonexistent/interpreter\n")
    g.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    held = s.run_guard("tr-x", True)
    assert held is not None and "launch-guard.sh" in held
