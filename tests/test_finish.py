"""finish.py: a worktree is finished when the session that recorded it leaves herdr. Real git repos
(with a bare remote) and a throwaway trk store; HOME is a temp folder, so no archive lands in ~/work."""
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from hx import finish
from hx.config import load_config
from hx.herdr import Pane, parse_panes
from hx.reconcile import reconcile
from hx.state import State
from hx.tracker import Trk, TrkError
from tests.conftest import FakeHerdr

NOW = 1_800_000_000.0
LATER = NOW + finish.DELAY_S


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null"}.items():
        monkeypatch.setenv(k, v)
    return tmp_path / "home"


def git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def wt(tmp_path, sh, trk_home):
    """A repo with a pushed main, a linked tree on branch feat (pushed), recorded by session s1."""
    remote, repo = tmp_path / "remote.git", tmp_path / "repo"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    git("init", "-q", "-b", "main", str(repo), cwd=tmp_path)
    (repo / ".gitignore").write_text(".env\n.venv/\ntmp/\n")
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    git("remote", "add", "origin", str(remote), cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    tree = repo / ".wt" / "a"
    git("worktree", "add", "-q", str(tree), "-b", "feat", cwd=repo)
    git("push", "-q", "origin", "feat", cwd=tree)
    (tree / "tmp").mkdir()
    (tree / "tmp" / "notes.md").write_text("notes\n")
    (tree / ".env").write_text("SECRET=1\n")
    (tree / ".venv").mkdir()
    (tree / ".venv" / "big").write_text("x" * 1000)
    prog = sh("new", "program", "P", "--criterion", "c")["item"]["id"]
    sh("edit", prog, "--archive", str(tmp_path / "arch" / "<tree>-tmp.tgz"))
    sh("worktree", "set", prog, "--path", str(tree), "--session", "s1")
    return {"repo": repo, "tree": tree, "item": prog, "archive": tmp_path / "arch" / "a-tmp.tgz",
            "trk": Trk(home=str(trk_home))}


def gone(wt, paths=None) -> State:
    s = State()
    s.finish["s1"] = {"paths": [str(p) for p in (paths or [wt["tree"]])], "since": NOW}
    return s


def never(repo, branch):
    return False


def test_a_clean_pushed_tree_is_archived_removed_and_cleared(wt, sh):
    s = gone(wt)
    lines, errors = finish.run(s, wt["trk"], [], "act", LATER, merged=never)
    assert errors == []
    assert not wt["tree"].exists()
    with tarfile.open(wt["archive"]) as t:
        names = t.getnames()
    assert "tmp/notes.md" in names and ".env" in names        # unique files are kept
    assert not any(n.startswith(".venv") for n in names)      # rebuildable ones are not
    assert "feat" in git("branch", "--list", "feat", cwd=wt["repo"])   # no merged PR: branch stays
    assert sh("show", wt["item"])["item"]["worktree"] is None
    assert lines[0].startswith(f"FINISHED  {wt['tree']}  {wt['item']}: archived 2 path(s)")
    assert "kept branch feat" in lines[0]
    assert s.finish == {}


def tip(wt, ref="feat"):
    return git("rev-parse", ref, cwd=wt["repo"]).strip()


def test_a_merged_pr_deletes_the_local_branch(wt):
    head = tip(wt)
    lines, _ = finish.run(gone(wt), wt["trk"], [], "act", LATER,
                          merged=lambda repo, b: head if b == "feat" else None)
    assert git("branch", "--list", "feat", cwd=wt["repo"]).strip() == ""
    assert "deleted branch feat (PR merged)" in lines[0]


def test_a_branch_with_commits_on_no_remote_is_never_deleted(wt):
    """Review C1: the tree moved to another branch; the recorded one has a local-only commit."""
    git("switch", "-q", "-c", "other", cwd=wt["tree"])
    git("push", "-q", "origin", "other", cwd=wt["tree"])
    sha = git("commit-tree", "HEAD^{tree}", "-p", "feat", "-m", "local only", cwd=wt["tree"]).strip()
    git("update-ref", "refs/heads/feat", sha, cwd=wt["repo"])
    lines, _ = finish.run(gone(wt), wt["trk"], [], "act", LATER, merged=lambda r, b: sha)
    assert tip(wt) == sha
    assert "kept branch feat" in lines[0]


def test_a_branch_whose_tip_is_not_the_merged_prs_is_kept(wt):
    """A reused branch name: the merged PR's head is some other commit."""
    lines, _ = finish.run(gone(wt), wt["trk"], [], "act", LATER, merged=lambda r, b: "0" * 40)
    assert tip(wt) and "kept branch feat" in lines[0]


def test_a_locked_tree_is_kept_and_never_archived(wt):
    """Review I1: a tree git will not remove is kept before anything is written."""
    git("worktree", "lock", str(wt["tree"]), cwd=wt["repo"])
    s = gone(wt)
    lines, _ = finish.run(s, wt["trk"], [], "act", LATER, merged=never)
    assert "it is locked" in lines[0] and not wt["archive"].exists() and s.finish == {}


def test_a_failed_remove_after_the_archive_is_final(wt, monkeypatch):
    """Review I1: one archive, one KEPT line, and no retry that archives again."""
    real = finish._git

    def no_remove(*a, cwd):
        if a[:2] == ("worktree", "remove"):
            raise RuntimeError("git worktree: refused")
        return real(*a, cwd=cwd)
    monkeypatch.setattr(finish, "_git", no_remove)
    s = gone(wt)
    lines, _ = finish.run(s, wt["trk"], [], "act", LATER, merged=never)
    assert s.finish == {} and wt["archive"].exists() and len(list(wt["archive"].parent.iterdir())) == 1
    assert lines[0].startswith(f"KEPT  {wt['tree']}") and "archived" in lines[0] and "refused" in lines[0]


def test_a_file_written_after_the_checks_stops_the_remove(wt, monkeypatch):
    """Review I2: the remove is git's own careful one, so new work in the tree is never deleted."""
    real = finish._archive

    def then_write(t, files, now):
        out = real(t, files, now)
        (wt["tree"] / "late.py").write_text("new work")
        return out
    monkeypatch.setattr(finish, "_archive", then_write)
    lines, _ = finish.run(gone(wt), wt["trk"], [], "act", LATER, merged=never)
    assert (wt["tree"] / "late.py").read_text() == "new work"
    assert lines[0].startswith("KEPT")


def test_a_failed_branch_delete_still_reports_the_finish(wt, monkeypatch):
    """Review I3: once the tree is gone the inbox says so, whatever fails after."""
    real = finish._git

    def no_delete(*a, cwd):
        if a[:1] == ("branch",):
            raise RuntimeError("git branch: refused")
        return real(*a, cwd=cwd)
    monkeypatch.setattr(finish, "_git", no_delete)
    lines, _ = finish.run(gone(wt), wt["trk"], [], "act", LATER, merged=lambda r, b: tip(wt))
    assert not wt["tree"].exists()
    assert lines[0].startswith("FINISHED") and "could not delete branch feat" in lines[0]


def test_a_pane_that_now_runs_another_session_keeps_the_tree(wt):
    """Review I4: /clear gives the pane a new session that may still work in the tree by path."""
    s = gone(wt)
    s.finish["s1"]["pane"] = "w1:p1"
    lines, _ = finish.run(s, wt["trk"], [Pane("w1:p1", "w1", "s-new", "idle", "/")], "act", LATER,
                          merged=never)
    assert wt["tree"].exists() and "its pane w1:p1 now runs session s-new" in lines[0]


def test_a_build_folder_below_the_top_is_archived(wt):
    """Review I5: only top-level build, dist and target folders count as rebuildable."""
    (wt["repo"] / ".gitignore").write_text(".env\n.venv/\ntmp/\nresults/build/\n")
    git("commit", "-qam", "ignore", cwd=wt["repo"])
    git("push", "-q", "origin", "main", cwd=wt["repo"])
    git("merge", "-q", "main", cwd=wt["tree"])
    git("push", "-q", "origin", "feat", cwd=wt["tree"])
    (wt["tree"] / "results" / "build").mkdir(parents=True)
    (wt["tree"] / "results" / "build" / "run1.csv").write_text("1\n")
    finish.run(gone(wt), wt["trk"], [], "act", LATER, merged=never)
    with tarfile.open(wt["archive"]) as t:
        assert "results/build/run1.csv" in t.getnames()


def test_one_tree_is_finished_per_pass(wt, sh):
    """Review I6: finishing never holds the pass for more than one tree."""
    b = wt["repo"] / ".wt" / "b"
    git("worktree", "add", "-q", str(b), "-b", "feat2", cwd=wt["repo"])
    git("push", "-q", "origin", "feat2", cwd=b)
    other = sh("new", "program", "P2", "--criterion", "c")["item"]["id"]
    sh("worktree", "set", other, "--path", str(b), "--session", "s2")
    s = gone(wt)
    s.finish["s2"] = {"paths": [str(b)], "since": NOW}
    finish.run(s, wt["trk"], [], "act", LATER, merged=never)
    assert [wt["tree"].exists(), b.exists()].count(False) == 1
    finish.run(s, wt["trk"], [], "act", LATER + 30, merged=never)
    assert not wt["tree"].exists() and not b.exists()


def test_a_bad_refusal_line_is_a_trk_error(tmp_path):
    fake = tmp_path / "trk"
    fake.write_text("#!/bin/sh\necho '{not json' >&2\nexit 1\n")
    fake.chmod(0o755)
    with pytest.raises(TrkError):
        Trk(bin=str(fake)).where("/x")


def test_commits_on_no_remote_keep_the_tree(wt):
    (wt["tree"] / "f").write_text("x")
    git("add", "f", cwd=wt["tree"])
    git("commit", "-q", "-m", "local", cwd=wt["tree"])
    lines, _ = finish.run(gone(wt), wt["trk"], [], "act", LATER, merged=never)
    assert wt["tree"].exists()
    assert "KEPT" in lines[0] and "1 commit(s) on no remote" in lines[0]


def test_uncommitted_changes_keep_the_tree(wt):
    (wt["tree"] / ".gitignore").write_text("changed\n")
    lines, _ = finish.run(gone(wt), wt["trk"], [], "act", LATER, merged=never)
    assert wt["tree"].exists() and "uncommitted changes" in lines[0]


def test_untracked_files_outside_tmp_keep_the_tree(wt):
    (wt["tree"] / "new.py").write_text("x")
    lines, _ = finish.run(gone(wt), wt["trk"], [], "act", LATER, merged=never)
    assert wt["tree"].exists() and "untracked files outside tmp/: new.py" in lines[0]


def test_a_live_pane_in_the_tree_keeps_it(wt):
    other = Pane("w1:p9", "w1", "s2", "idle", str(wt["tree"] / "sub"))
    lines, _ = finish.run(gone(wt), wt["trk"], [other], "act", LATER, merged=never)
    assert wt["tree"].exists() and "a live pane works in it" in lines[0]


def test_an_archive_over_the_limit_keeps_the_tree(wt, monkeypatch):
    monkeypatch.setattr(finish, "MAX_BYTES", 5)
    lines, _ = finish.run(gone(wt), wt["trk"], [], "act", LATER, merged=never)
    assert wt["tree"].exists() and "its archive would be" in lines[0]


def test_a_tree_another_session_recorded_is_left_alone_quietly(wt, sh):
    sh("worktree", "set", wt["item"], "--path", str(wt["tree"]), "--session", "s-other")
    lines, _ = finish.run(gone(wt), wt["trk"], [], "act", LATER, merged=never)
    assert wt["tree"].exists() and lines == []


def test_an_unrecorded_tree_the_session_worked_in_is_reported(wt, sh):
    sh("worktree", "clear", wt["item"])
    lines, _ = finish.run(gone(wt), wt["trk"], [], "act", LATER, merged=never)
    assert wt["tree"].exists()
    assert lines == [f"KEPT  {wt['tree']}  worktree of session s1: no trk item records it with this "
                     f"session; finish it by hand"]


def test_report_mode_changes_nothing(wt, sh):
    lines, _ = finish.run(gone(wt), wt["trk"], [], "report", LATER, merged=never)
    assert wt["tree"].exists() and not wt["archive"].exists()
    assert sh("show", wt["item"])["item"]["worktree"] is not None
    assert lines[0].startswith(f"WOULD FINISH  {wt['tree']}")


def test_off_mode_drops_the_candidates(wt):
    s = gone(wt)
    assert finish.run(s, wt["trk"], [], "off", LATER, merged=never) == ([], [])
    assert s.finish == {} and wt["tree"].exists()


def test_nothing_happens_before_the_delay(wt):
    s = gone(wt)
    assert finish.run(s, wt["trk"], [], "act", LATER - 1, merged=never) == ([], [])
    assert "s1" in s.finish and wt["tree"].exists()


def test_a_session_that_came_back_is_not_finished(wt):
    s = gone(wt)
    back = Pane("w1:p1", "w1", "s1", "idle", "/")
    finish.run(s, wt["trk"], [back], "act", LATER, merged=never)
    assert s.finish == {} and wt["tree"].exists()


def test_the_tree_is_found_from_the_main_checkout_too(wt):
    """Claude's pane may sit at the repo root while its tree is .wt/a: every linked tree is tried."""
    finish.run(gone(wt, [wt["repo"]]), wt["trk"], [], "act", LATER, merged=never)
    assert not wt["tree"].exists()


def test_an_existing_archive_is_never_overwritten(wt):
    wt["archive"].parent.mkdir(parents=True)
    wt["archive"].write_text("old")
    lines, _ = finish.run(gone(wt), wt["trk"], [], "act", LATER, merged=never)
    assert wt["archive"].read_text() == "old"
    assert "a-tmp-20270115T080500Z.tgz" in lines[0]


def test_the_default_archive_is_under_work_archive(home):
    assert finish.expand(None, "/x/work-ycfsf") == f"{home}/work/archive/work-ycfsf-tmp.tgz"


def test_a_trk_failure_keeps_the_candidate_for_the_next_pass(wt):
    class Down:
        def where(self, path):
            raise TrkError("trk where: timed out")
    s = gone(wt)
    lines, errors = finish.run(s, Down(), [], "act", LATER, merged=never)
    assert lines == [] and "timed out" in errors[0] and "s1" in s.finish
    lines, _ = finish.run(s, Down(), [], "act", NOW + finish.GIVE_UP_S + 1, merged=never)
    assert "failed for a day" in lines[0] and s.finish == {}


def test_a_session_that_left_herdr_becomes_a_candidate():
    s = State(seen={"s1": "w1:p1", "s2": "w1:p2"})
    finish.remember(s, [Pane("w1:p1", "w1", "s1", "idle", "/repo", fg_cwd="/repo/.wt/a"),
                        Pane("w1:p2", "w1", "s2", "idle", "/r2")])
    before = dict(s.seen)
    s.seen = {"s2": "w1:p2"}
    finish.queue(s, before, NOW)
    assert s.finish == {"s1": {"paths": ["/repo/.wt/a", "/repo"], "since": NOW, "pane": "w1:p1"}}
    assert set(s.places) == {"s2"}


def test_panes_carry_the_foreground_folder():
    reply = {"result": {"panes": [{"pane_id": "w1:p1", "workspace_id": "w1", "cwd": "/r",
                                   "foreground_cwd": "/r/.wt/a"}]}}
    assert parse_panes(reply)[0].fg_cwd == "/r/.wt/a"


def test_the_mode_comes_from_config(tmp_path):
    p = tmp_path / "c.toml"
    assert load_config(p).finish == "act"
    p.write_text('[worktrees]\nfinish = "report"\n')
    assert load_config(p).finish == "report"
    p.write_text('[worktrees]\nfinish = "sometimes"\n')
    with pytest.raises(ValueError, match="act, report or off"):
        load_config(p)


def test_a_pass_finishes_the_tree_of_a_session_that_closed(wt, trk_home, tmp_path, monkeypatch):
    monkeypatch.setattr(finish, "gh_merged", never)
    pane = Pane("w1:p1", "w1", "s1", "idle", str(wt["repo"]), fg_cwd=str(wt["tree"]))
    keep = Pane("w1:p2", "w1", "s2", "idle", "/")

    def run(panes, now):
        reconcile(wt["trk"], FakeHerdr(panes), tmp_path / "state.json", tmp_path / "inbox.log",
                  tmp_path / "q", {"michael": ["console"]}, now=now)
    run([pane, keep], NOW)
    run([keep], NOW + 1)                       # s1's pane closed
    assert wt["tree"].exists()                 # not before the delay
    run([keep], NOW + 1 + finish.DELAY_S)
    assert not wt["tree"].exists()
    assert "FINISHED" in (tmp_path / "inbox.log").read_text()
    assert json.loads((tmp_path / "state.json").read_text())["finish"] == {}
