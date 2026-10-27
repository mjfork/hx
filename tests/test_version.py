"""`hx --version`: the version and the commit hx came from, for an installer asking a box what it runs. An
editable hx reads its checkout live; a `--no-editable` copy reports the sha hatch_build.py baked into its wheel.

The wheel tests build from a committed copy of this working tree, so they name a sha of their own and do not
depend on whether the checkout they run from is clean."""

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from hx import version
from hx.cli import main

REPO = Path(__file__).parent.parent
SKIP = shutil.ignore_patterns(".venv", "__pycache__", "*.pyc", "dist", "tmp", ".worktrees", "*.bundle")


def git(tree: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def test_version_names_the_checkout_hx_runs_from(capsys):
    """The dev venv's hx is an editable install, so this is the editable and run-from-checkout path."""
    assert main(["--version", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "schema": 1, "name": "hx", "version": "0.1.0", "commit": git(REPO, "rev-parse", "HEAD"),
        "dirty": bool(git(REPO, "status", "--porcelain")),
    }


def test_the_version_line_names_the_short_sha_and_whether_the_tree_was_clean():
    sha = "175e440a8c7341eeb9e1161ae160e7608ce5ae94"
    assert version.text({"name": "hx", "version": "0.1.0", "commit": sha, "dirty": False}) == "hx 0.1.0 (175e440, clean)"
    assert version.text({"name": "hx", "version": "0.1.0", "commit": sha, "dirty": True}) == "hx 0.1.0 (175e440, dirty)"
    assert version.text({"name": "hx", "version": "0.1.0", "commit": None, "dirty": False}) == "hx 0.1.0 (commit unknown)"


def test_version_takes_only_json(capsys):
    assert main(["--version", "--commit"]) == 2
    assert "takes only --json, not --commit" in capsys.readouterr().err


def test_a_checkout_carries_no_baked_stamp():
    """Nothing is written into the working tree, so an editable hx keeps reading git live."""
    assert version.baked() == (None, False)
    assert not (REPO / "src/hx/_build.py").exists()


def test_a_copy_outside_a_checkout_names_no_commit(tmp_path):
    assert version.checkout_head(tmp_path) == (None, False)


def test_a_repo_that_merely_surrounds_an_installed_copy_is_not_reported(tmp_path):
    """An installed copy under a git $HOME (dotfiles) must not claim that repo's HEAD as the hx it came from."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "f").write_text("x\n")
    git(tmp_path, "add", "f")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "f")
    copy = tmp_path / "lib/python3.12/site-packages/hx"
    copy.mkdir(parents=True)

    assert version.checkout_head(copy) == (None, False)


@pytest.fixture(scope="module")
def committed(tmp_path_factory):
    """A copy of this working tree with everything committed: a checkout of its own, clean, with its own sha."""
    tree = tmp_path_factory.mktemp("hx-copy") / "hx"
    shutil.copytree(REPO, tree, ignore=SKIP)
    git(tree, "add", "-A")
    # --allow-empty: the tree under test is as often clean as dirty, and the copy needs a commit of its own either way
    git(tree, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty",
        "-m", "the tree under test")
    assert git(tree, "status", "--porcelain") == ""
    return tree


def stamp(wheel: Path) -> dict:
    """COMMIT and DIRTY as hatch_build.py wrote them into the wheel's hx/_build.py."""
    with zipfile.ZipFile(wheel) as z:
        text = z.read("hx/_build.py").decode()
    scope: dict = {}
    exec(text, scope)
    return {"commit": scope.get("COMMIT"), "dirty": scope.get("DIRTY")}


def build(tree: Path, out: Path) -> Path:
    done = subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out)], cwd=tree, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    return next(out.glob("*.whl"))


def test_a_wheel_carries_the_commit_it_was_built_from(committed, tmp_path):
    assert stamp(build(committed, tmp_path / "dist")) == {"commit": git(committed, "rev-parse", "HEAD"), "dirty": False}


def test_a_wheel_says_so_when_the_checkout_was_dirty(committed, tmp_path):
    tree = tmp_path / "hx"
    shutil.copytree(committed, tree)
    (tree / "README.md").write_text("changed\n")

    assert stamp(build(tree, tmp_path / "dist")) == {"commit": git(tree, "rev-parse", "HEAD"), "dirty": True}


def test_a_non_editable_install_reports_the_sha_it_was_built_from(committed, tmp_path):
    """What the gatehouse installer reads back: `hx --version --json` from a copy with no git anywhere above it."""
    wheel = build(committed, tmp_path / "dist")
    venv = tmp_path / "venv"
    subprocess.run(["uv", "venv", "-q", str(venv)], check=True)
    done = subprocess.run(["uv", "pip", "install", "-q", "--python", str(venv / "bin/python"), str(wheel)],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not (venv / ".git").exists()

    run = subprocess.run([str(venv / "bin/hx"), "--version", "--json"], cwd=tmp_path, capture_output=True, text=True,
                         env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})   # no checkout, and git is still there

    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(run.stdout) == {"schema": 1, "name": "hx", "version": "0.1.0",
                                      "commit": git(committed, "rev-parse", "HEAD"), "dirty": False}
