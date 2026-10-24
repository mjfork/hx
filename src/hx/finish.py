"""Finish a worktree when the session that recorded it leaves herdr (hx-improve-1 item 4).

A finish archives tmp/ and every ignored file that cannot be rebuilt, removes the tree with git's
careful remove, deletes the local branch when it is the merged PR's head and is on a remote, and
clears the trk record. A tree is kept, and reported, when it has uncommitted changes, untracked files
outside tmp/, commits on no remote, a live pane in it, is locked, belongs to another repo, its
session's pane now runs another session, its archive would pass MAX_BYTES, or no trk item records it
with this session. One tree is finished per pass, so the pass is never held for long.
"""
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from hx.tracker import TrkError

DEFAULT_ARCHIVE = "~/work/archive/<tree>-tmp.tgz"
REBUILD_ANYWHERE = {".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
                    ".ruff_cache"}
REBUILD_AT_TOP = {".tox", "target", "dist", "build", ".next", ".gradle", ".cache"}
MAX_BYTES = 500 * 2**20
DELAY_S = 300            # a session must stay gone this long: a herdr restart brings it back sooner
GIVE_UP_S = 86400        # a candidate that keeps failing for a day is dropped, with a note
MODES = ("act", "report", "off")


class Keep(Exception):
    """The tree stays where it is; the message says why."""


@dataclass(frozen=True)
class Tree:
    path: str
    item: str
    branch: str | None
    repo: str
    archive: str          # the expanded destination


def _git(*args: str, cwd: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"git {args[0]}: {out.stderr.strip()[:200]}")
    return out.stdout


def gh_merged(repo: str, branch: str) -> str | None:
    """The head commit of a merged PR from `branch`, or None. Any doubt (no gh, no PR, no network) is None."""
    try:
        out = subprocess.run(["gh", "pr", "list", "--head", branch, "--state", "merged", "--json",
                              "headRefOid", "--limit", "1"], cwd=repo, capture_output=True, text=True,
                             timeout=60)
        prs = json.loads(out.stdout) if out.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return prs[0].get("headRefOid") if prs and isinstance(prs[0], dict) else None


def remember(state, panes) -> None:
    """The folders each live session works in, so its trees can be found once it has gone."""
    for p in panes:
        if p.session:
            places = [d for d in (p.fg_cwd, p.cwd) if d]
            if places:
                state.places[p.session] = list(dict.fromkeys(places + state.places.get(p.session, [])))[:4]


def queue(state, before: dict, now: float) -> None:
    """Sessions in the last pass's herdr snapshot and not in this one become candidates."""
    for session in set(before) - set(state.seen):
        if state.places.get(session):
            state.finish[session] = {"paths": state.places[session], "since": now, "pane": before[session]}
    for session in list(state.places):
        if session not in state.seen:
            state.places.pop(session)


def expand(template: str | None, tree: str) -> str:
    return os.path.expanduser((template or DEFAULT_ARCHIVE).replace("<tree>", Path(tree).name))


def free_name(dest: str, now: float) -> str:
    """dest, or dest with a time (and a count) before .tgz when an archive is there: never overwrite."""
    if not os.path.exists(dest):
        return dest
    stem, ext = (dest[:-4], ".tgz") if dest.endswith(".tgz") else os.path.splitext(dest)
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))
    name, n = f"{stem}-{stamp}{ext}", 1
    while os.path.exists(name):
        n += 1
        name = f"{stem}-{stamp}-{n}{ext}"
    return name


def _worktrees(path: str) -> list[dict]:
    """`git worktree list --porcelain` of the repo holding `path`, one dict per tree, main first."""
    out = _git("worktree", "list", "--porcelain", cwd=path)
    trees, cur = [], None
    for line in out.splitlines():
        if line.startswith("worktree "):
            cur = {"path": os.path.realpath(line[len("worktree "):])}
            trees.append(cur)
        elif cur is not None and line:
            k, _, v = line.partition(" ")
            cur[k] = v or True
    return trees


def linked_trees(path: str) -> list[str]:
    """The linked worktrees of the repo that holds `path` (never the main checkout)."""
    try:
        return [t["path"] for t in _worktrees(path)[1:]]
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return []


def _status(tree: str) -> list[tuple[str, str]]:
    out = _git("status", "--porcelain=v1", "-z", "--ignored=matching", "--untracked-files=all", cwd=tree)
    return [(e[:2], e[3:]) for e in out.split("\0") if e]


def _rebuildable(rel: str) -> bool:
    parts = Path(rel).parts
    return bool(parts) and (parts[0] in REBUILD_AT_TOP or any(p in REBUILD_ANYWHERE for p in parts)
                            or rel.endswith(".pyc"))


def _size(tree: str, rel: str) -> int:
    p = Path(tree, rel)
    if p.is_file() or p.is_symlink():
        return p.lstat().st_size
    return sum(f.lstat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())


def _common_dir(path: str) -> str:
    return os.path.realpath(os.path.join(path, _git("rev-parse", "--git-common-dir", cwd=path).strip()))


def plan(t: Tree, live_dirs: list[str]) -> tuple[list[str], dict]:
    """What to archive (paths relative to the tree) and the tree's git record, or Keep with the reason."""
    if any(d == t.path or d.startswith(t.path + os.sep) for d in live_dirs):
        raise Keep("a live pane works in it")
    rec = next((w for w in _worktrees(t.repo) if w["path"] == t.path), None)
    if rec is None or _common_dir(t.path) != _common_dir(t.repo):
        raise Keep(f"it is not a worktree of {t.repo}")
    if "locked" in rec:
        raise Keep("it is locked")
    keep, untracked = [], []
    for code, rel in _status(t.path):
        if code == "!!":
            if not _rebuildable(rel) and not (rel == "tmp" or rel.startswith("tmp/")):
                keep.append(rel.rstrip("/"))
        elif code == "??":
            if not (rel == "tmp" or rel.startswith("tmp/")):
                untracked.append(rel)
        else:
            raise Keep("it has uncommitted changes")
    if untracked:
        raise Keep(f"untracked files outside tmp/: {', '.join(untracked[:3])}")
    ahead = _git("rev-list", "--count", "HEAD", "--not", "--remotes", cwd=t.path).strip()
    if ahead != "0":
        raise Keep(f"{ahead} commit(s) on no remote")
    files = (["tmp"] if os.path.lexists(os.path.join(t.path, "tmp")) else []) + keep
    total = sum(_size(t.path, f) for f in files)
    if total > MAX_BYTES:
        raise Keep(f"its archive would be {total // 2**20} MB (over {MAX_BYTES // 2**20} MB)")
    return files, rec


def _archive(t: Tree, files: list[str], now: float) -> str:
    """Write the archive (never over another), then take the archived paths out of the tree."""
    dest = free_name(t.archive, now)
    if not os.path.isabs(dest):
        raise Keep(f"its archive path {dest} is not absolute")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        out = subprocess.run(["tar", "czf", dest, "-C", t.path, "--null", "-T", "-"],
                             input="\0".join(files) + "\0", capture_output=True, text=True, timeout=300)
        ok = out.returncode == 0
        err = out.stderr.strip()[:200]
    except subprocess.TimeoutExpired:
        ok, err = False, "timed out"
    if not ok:
        Path(dest).unlink(missing_ok=True)
        raise RuntimeError(f"tar: {err}")
    return dest


def _drop(t: Tree, files: list[str]) -> None:
    """Take the archived paths out of the tree, so git's careful remove sees only rebuildable files."""
    for rel in files:
        p = Path(t.path, rel)
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink(missing_ok=True)


def _delete_branch(t: Tree, rec: dict, merged) -> str:
    """Delete the branch only when the tree had it checked out, every commit of it is on a remote, and
    its tip is the merged PR's head or behind it."""
    if not t.branch:
        return ""
    if rec.get("branch") != f"refs/heads/{t.branch}":
        return f"kept branch {t.branch} (not checked out in the tree)"
    try:
        if _git("rev-list", "--count", t.branch, "--not", "--remotes", cwd=t.repo).strip() != "0":
            return f"kept branch {t.branch} (commits on no remote)"
        head = merged(t.repo, t.branch)
        tip = _git("rev-parse", t.branch, cwd=t.repo).strip()
        if not head or (tip != head and subprocess.run(
                ["git", "merge-base", "--is-ancestor", tip, head], cwd=t.repo,
                capture_output=True, timeout=60).returncode != 0):
            return f"kept branch {t.branch} (no merged PR at its tip)"
        _git("branch", "-D", t.branch, cwd=t.repo)
        return f"deleted branch {t.branch} (PR merged)"
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        return f"could not delete branch {t.branch}: {e}"


def finish(t: Tree, files: list[str], rec: dict, trk, merged, now: float) -> str:
    """Archive, remove, then the branch and the trk record. Returns the note; raises Keep when the
    tree stays (after an archive, the Keep names it, so the next pass never archives again)."""
    dest = _archive(t, files, now) if files else None
    done = f"archived {len(files)} path(s) to {dest}" if dest else "nothing to archive"
    try:
        _drop(t, files)
        _git("worktree", "remove", t.path, cwd=t.repo)     # no --force: git refuses new work
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        raise Keep(f"{done}, but the remove failed: {e}") from None
    parts = [done, "removed"]
    note = _delete_branch(t, rec, merged)
    if note:
        parts.append(note)
    try:
        trk.worktree_clear(t.item)
    except TrkError as e:
        parts.append(f"trk record not cleared: {e}")
    return ", ".join(parts)


OTHER = "other"     # _tree: a record exists, by another session


def _tree(trk, path: str, session: str):
    """The tree's newest trk record if this session made it; OTHER if another did; None if none."""
    w = trk.where(path)
    if w is None:
        return None
    row = w["items"][0]
    wt = row["worktree"] or {}
    if wt.get("session") != session:
        return OTHER
    return Tree(w["tree"], row["id"], wt.get("branch"), wt.get("repo") or path,
                expand(row.get("effective_archive"), w["tree"]))


def run(state, trk, panes, mode: str, now: float, merged=None) -> tuple[list[str], list[str]]:
    """One pass over the candidates. Returns (inbox lines, errors for watch.log)."""
    merged = merged or gh_merged
    lines, errors = [], []
    if mode == "off":
        state.finish.clear()
        return lines, errors
    live = {p.session for p in panes if p.session}
    by_pane = {p.pane_id: p for p in panes}
    live_dirs = [os.path.realpath(d) for p in panes for d in (p.cwd, p.fg_cwd) if d]
    acted = False
    for session, cand in list(state.finish.items()):
        if session in live:                   # it came back (a herdr restart): nothing to finish
            state.finish.pop(session)
            continue
        if now - cand["since"] < DELAY_S or acted:
            continue
        own = {os.path.realpath(p) for p in cand["paths"]}
        trees = list(dict.fromkeys(t for p in cand["paths"] for t in linked_trees(p)))
        failed = False
        for path in trees:
            key = f"finish:{session}:{path}"
            if key in state.done:
                continue
            if acted:
                failed = True                 # the next pass takes the rest
                break
            try:
                t = _tree(trk, path, session)
                if t is OTHER:
                    state.done[key] = _iso(now)
                    continue
                if t is None:
                    if any(o == path or o.startswith(path + os.sep) for o in own):
                        lines.append(f"KEPT  {path}  worktree of session {session}: no trk item records it "
                                     f"with this session; finish it by hand")
                    state.done[key] = _iso(now)
                    continue
                try:
                    pane = by_pane.get(cand.get("pane"))
                    if pane is not None and pane.session:
                        raise Keep(f"its pane {pane.pane_id} now runs session {pane.session}")
                    files, rec = plan(t, live_dirs)
                    if mode == "report":
                        lines.append(f"WOULD FINISH  {t.path}  {t.item}: archive {len(files)} path(s) to "
                                     f"{t.archive}, remove the tree, delete branch {t.branch} if its PR "
                                     f"merged")
                    else:
                        acted = True
                        lines.append(f"FINISHED  {t.path}  {t.item}: {finish(t, files, rec, trk, merged, now)}")
                except Keep as k:
                    lines.append(f"KEPT  {t.path}  {t.item}: {k}; finish it by hand")
                state.done[key] = _iso(now)
            except (TrkError, RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as e:
                errors.append(f"{path}: {type(e).__name__}: {e}")
                failed = True
        if not failed:
            state.finish.pop(session)
        elif now - cand["since"] > GIVE_UP_S:
            lines.append(f"KEPT  session {session}: finishing its trees failed for a day; see watch.log")
            state.finish.pop(session)
    return lines, errors


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
