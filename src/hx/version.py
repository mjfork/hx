"""`hx --version`: the package version and the commit of the checkout this hx came from. Imports nothing else from
hx, so it answers with no config, no trk store, no herdr and no watcher."""

import subprocess
from importlib.metadata import PackageNotFoundError, version as dist_version
from pathlib import Path

SOURCE = Path(__file__).resolve().parent   # <checkout>/src/hx when hx runs from one; site-packages/hx when installed


def report() -> dict:
    """What `hx --version --json` prints. `commit` is the full sha of the checkout the install came from, or None
    when hx was built outside one; `dirty` says that checkout had uncommitted changes."""
    commit, dirty = baked()
    if commit is None:
        commit, dirty = checkout_head(SOURCE)
    return {"schema": 1, "name": "hx", "version": package_version(), "commit": commit, "dirty": dirty}


def text(reply: dict) -> str:
    """One line for a person: `hx 0.1.0 (175e440, clean)`."""
    commit = reply["commit"]
    where = f"{commit[:7]}, {'dirty' if reply['dirty'] else 'clean'}" if commit else "commit unknown"
    return f"hx {reply['version'] or 'unknown'} ({where})"


def package_version():
    try:
        return dist_version("hx")
    except PackageNotFoundError:       # run from a checkout that was never installed
        return None


def baked():
    """What hatch_build.py wrote into the wheel: a `--no-editable` copy's sha, since its tree carries no git."""
    try:
        from hx import _build
    except ImportError:
        return None, False
    commit = getattr(_build, "COMMIT", None)
    if not isinstance(commit, str) or len(commit) != 40:
        return None, False
    return commit, bool(getattr(_build, "DIRTY", False))


def checkout_head(source: Path):
    """HEAD of the checkout `source` is the src/hx of, and whether it has uncommitted changes: the editable install
    and a run from the checkout. (None, False) otherwise, so a repo that merely surrounds an installed copy (a
    dotfiles $HOME, say) is never reported as the hx it came from."""
    top = git(source, "rev-parse", "--show-toplevel")
    if top is None or Path(top).resolve() != source.parent.parent:
        return None, False
    sha = git(source, "rev-parse", "HEAD")
    if sha is None or len(sha) != 40:
        return None, False
    return sha, bool(git(source, "status", "--porcelain"))


def git(source: Path, *args: str):
    try:
        done = subprocess.run(["git", "-C", str(source), *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None
