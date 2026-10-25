"""hx start <root>: open the Space for a root and start its solo session (spec Section 2,
amendments 2, 3, 6 and 7). The only opener; orchestrator Spaces arrive in slice 3b."""
import copy
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from hx.config import Config
from hx.herdr import HerdrError
from hx.spaces import iso
from hx.state import load, locked, save, state_dir
from hx.tracker import Trk

MODEL_RE = re.compile(r"^[][A-Za-z0-9._-]+$")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
JIRA_KEY = re.compile(r"[A-Z][A-Z0-9]+-[0-9]+", re.I)            # the accepted forms:
JIRA_BROWSE = re.compile(r"atlassian\.net/browse/([A-Z][A-Z0-9]+-[0-9]+)", re.I)   # a bare key or /browse/<key>
DEPS_LOOP = ('for i in $(seq 120); do [[ -f "$1/package-lock.json" ]] && { sleep 5; exec ticket-deps.sh "$1"; }; '
             'sleep 1; done; echo "worktree $1 did not appear" >&2; exit 1')


class StartError(Exception):
    pass


OVERRIDES = ("HERDR_HOLD_OVERRIDE", "DISPATCH_FORCE")   # launch-guard.sh's overrides


def run_guard(label: str, check_only: bool) -> str | None:
    """launch-guard.sh on PATH (scripts/launch-guard.sh; install.sh --launch-guard): None when the
    box can take a session, else why not.
    A machine with no launch-guard.sh has no guard: every launch goes ahead."""
    if not any(os.path.exists(os.path.join(d, "launch-guard.sh"))     # present at all, runnable or not
               for d in os.environ.get("PATH", "").split(os.pathsep) if d):
        return None
    args = ["launch-guard.sh", *(["--check-only"] if check_only else []), label]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except FileNotFoundError as e:       # on PATH but unrunnable (a missing interpreter): hold
        return f"launch-guard.sh cannot run: {e}"
    except OSError as e:
        return f"launch-guard.sh cannot run: {e}"
    except subprocess.TimeoutExpired:
        return "launch-guard.sh timed out"
    if out.returncode == 0:
        return None
    if out.returncode == 3:
        lines = out.stderr.strip().splitlines()
        return lines[-1] if lines else "launch-guard.sh held the launch"
    return f"launch-guard exit {out.returncode}"


def spawn_deps(worktree: str, log: Path) -> None:
    """Fill the new worktree's dependencies in the background with ticket-deps.sh, an optional
    local script that takes the worktree's path. Nothing to do on a machine without one."""
    if not shutil.which("ticket-deps.sh"):
        return
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w") as f:
        subprocess.Popen(["bash", "-c", DEPS_LOOP, "_", worktree], stdout=f, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)


def _close_by_label(herdr, label: str, before: set[str]) -> str:
    """herdr may have made the workspace before its reply failed: close one with this label that was
    not there before the create."""
    try:
        made = [w.workspace_id for w in herdr.workspaces()
                if w.label.rstrip() == label and w.workspace_id not in before]
        for w in made:
            herdr.close_space(w)
    except (HerdrError, KeyError, ValueError) as e:
        return f" (could not check for a half-made Space: {e})"
    return f" (closed {', '.join(made)})" if made else ""


def _jira_key(url: str) -> str | None:
    if JIRA_KEY.fullmatch(url.strip()):
        return url.strip()
    m = JIRA_BROWSE.search(url)
    return m.group(1) if m else None


def _checkout(repos: list[str]) -> str | None:
    for r in repos:
        path = os.path.expanduser(r)
        if os.path.exists(os.path.join(path, ".git")):
            return path
    return None


def start(trk: Trk, herdr, state_path: Path, cfg: Config, root: str, now: float | None = None, *,
          model: str | None = None, effort: str = "medium", reason: str | None = None,
          parent: str | None = None, dry_run: bool = False, guard=run_guard, spawn=spawn_deps) -> str:
    it = trk.show(root)["item"]
    root = it["id"]                      # trk's own spelling, so the watcher's claims match the binding
    if it["parent"]:
        raise StartError(f"{root} is not a root (its parent is {it['parent']}); start its root instead")
    if it["status"] != "open":
        raise StartError(f"{root} is {it['status']}")
    if any(c["form"] != "ask" and c["status"] == "open" for c in it["children"]):
        raise StartError(f"{root} has open children: orchestrator Spaces arrive in slice 3b; "
                         "drive it with trk-drive-program for now")
    if it["kind"] != "work-item" or it["form"] != "self-contained":
        what = it["kind"] + (f" ({it['form']})" if it["form"] else "")
        raise StartError(f"{root} is a {what}: a solo session works only a self-contained work item "
                         "through the gates")
    role = "solo"
    repo = _checkout(it.get("repos") or [])
    if repo is None:
        raise StartError(f"{root} names no repo checkout; add one with `trk edit {root} --add-repo <path>`")
    model = cfg.models[role] if model is None else model
    if not MODEL_RE.fullmatch(model):
        raise StartError(f"bad model: {model}")
    if effort not in EFFORTS:
        raise StartError(f"bad effort: {effort} ({'|'.join(EFFORTS)})")
    if reason is not None:
        reason = reason.strip()
        if not reason or "\n" in reason or "\r" in reason:
            raise StartError("--reason must be one non-empty line")
    if "opus" in model.lower() and not reason:
        raise StartError(f"{model} needs --reason (routing-1: choosing opus needs a one-line reason)")
    worktree = (_jira_key(it.get("jira_url") or "") or root).lower()   # only [a-z0-9-]: it goes into a shell line
    command = cfg.commands[role].format(model=model, effort=effort, worktree=worktree, root=root)
    title = it["title"] if len(it["title"]) <= 40 else it["title"][:39] + "…"
    label = f"{root} {title}"
    env = {"HX_ROLE": role, "HX_ROOT": root}
    # An override that let this launch past the guard must reach the pane too, or claude-worker's own
    # guard refuses there and leaves an empty Space. claude-worker unsets them for the session.
    env.update({k: os.environ[k] for k in OVERRIDES if os.environ.get(k)})
    with locked(state_path):
        state = copy.deepcopy(load(state_path))
        b = state.bindings.get(root)
        try:
            before = {w.workspace_id for w in herdr.workspaces()}
        except (HerdrError, KeyError, ValueError) as e:
            raise StartError(f"herdr could not list workspaces: {e}") from e
        if b and b["workspace"] in before:
            return f"{root} already open in {b['workspace']}"
        if os.path.exists(f"{repo}/.claude/worktrees/{worktree}"):
            raise StartError(f"{repo}/.claude/worktrees/{worktree} already exists (an earlier dispatch or hx start?); "
                             "finish or remove it with `git worktree remove` first")
        if dry_run:
            held = guard(root, True)
            return (f"would open {label!r} in {repo} with {env} and run: {command}\n"
                    + (f"guard held: {held}" if held else "guard ok"))
        held = guard(root, False)
        if held:
            raise StartError(f"held: {held}")
        try:
            c = herdr.create_space(label, repo, env)
        except (HerdrError, KeyError, ValueError) as e:
            raise StartError(f"herdr could not create the Space: {e}{_close_by_label(herdr, label, before)}") from e
        try:
            herdr.rename_tab(c.tab_id, role)
            herdr.run(c.pane_id, command)
        except HerdrError as e:
            try:
                herdr.close_space(c.workspace_id)
                closed = "closed it"
            except HerdrError as e2:
                closed = f"could not close {c.workspace_id}: {e2}"
            raise StartError(f"herdr failed after opening {c.workspace_id} ({closed}): {e}") from e
        state.bindings[root] = {"workspace": c.workspace_id, "origin": "hx", "orchestrator": None,
                                "role": role, "pane": c.pane_id, "tab": c.tab_id,
                                "opened_at": iso(time.time() if now is None else now), "idle_since": None,
                                "launch": {"model": model, "effort": effort, "reason": reason,
                                           "parent": parent}}
        save(state, state_path)
    note = ""
    if not os.environ.get("DISPATCH_NO_DEPS"):
        try:
            spawn(f"{repo}/.claude/worktrees/{worktree}", state_dir() / "deps" / f"{worktree}.log")
        except OSError as e:             # the session runs anyway; it can install its own deps
            note = f"; deps not started: {e}"
    return f"{root} opened in {c.workspace_id} as {role} ({model}/{effort}){note}"
