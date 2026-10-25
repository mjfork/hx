"""The opt-in extras: scripts/launch-guard.sh, the mem-gate and checkout-freshness hooks, and
scripts/merge_settings.py. Each runs as Claude Code or hx start would run it, against tmp files."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
GUARD = REPO / "scripts/launch-guard.sh"
MEM_GATE = REPO / "claude/hooks/mem-gate.sh"
FRESHNESS = REPO / "claude/hooks/checkout-freshness.sh"
MERGE = REPO / "scripts/merge_settings.py"

linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="launch-guard.sh reads /proc and uses flock")
needs_jq = pytest.mark.skipif(not shutil.which("jq"), reason="the hooks speak JSON through jq")


def meminfo(path, avail_mb, swap_total_kb=0, swap_free_kb=0):
    path.write_text(f"MemTotal: 99999999 kB\nMemAvailable: {avail_mb * 1024} kB\n"
                    f"SwapTotal: {swap_total_kb} kB\nSwapFree: {swap_free_kb} kB\n")
    return path


# --- launch-guard.sh ---

@pytest.fixture
def guard(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state"),
           "HERDR_HOLD_FILE": str(tmp_path / "hold.json"), "DISPATCH_MEMINFO": str(meminfo(tmp_path / "mi", 65536)),
           "DISPATCH_MAX_LOAD": "100000", "DISPATCH_GAP_SEC": "0", "DISPATCH_MAX_DISK_PCT": "101"}

    def run(*args, **extra):
        return subprocess.run(["bash", str(GUARD), *args], capture_output=True, text=True, env={**env, **extra})
    run.stamp = tmp_path / "state/launch-guard.last"
    run.tmp = tmp_path
    return run


def test_guard_without_a_label_prints_usage(guard):
    out = guard()
    assert out.returncode == 2 and "launch-guard.sh" in out.stderr


@linux
def test_guard_lets_a_launch_through_and_records_it(guard):
    out = guard("tr-abc12")
    assert out.returncode == 0, out.stderr
    assert guard.stamp.read_text().strip().isdigit()


@linux
def test_check_only_records_nothing(guard):
    assert guard("--check-only", "tr-abc12").returncode == 0
    assert not guard.stamp.exists()


@linux
def test_a_launch_inside_the_gap_waits(guard):
    assert guard("tr-a").returncode == 0
    out = guard("tr-b", DISPATCH_GAP_SEC="3600")
    assert out.returncode == 3 and "last launch was" in out.stderr and "tr-b" in out.stderr


@linux
def test_short_memory_holds_the_launch(guard):
    out = guard("tr-abc12", DISPATCH_MEMINFO=str(meminfo(guard.tmp / "low", 512)))
    assert out.returncode == 3 and "only 512 MB memory available" in out.stderr
    assert not guard.stamp.exists()


@linux
def test_full_swap_holds_even_a_hold_only_check(guard):
    full = meminfo(guard.tmp / "swap", 65536, swap_total_kb=1000, swap_free_kb=10)
    out = guard("--hold-only", "wrapper", DISPATCH_MEMINFO=str(full))
    assert out.returncode == 3 and "swap 99%" in out.stderr


def test_a_machine_wide_hold_holds_and_force_does_not_skip_it(guard):
    hold = {"id": "h1", "reason": "disk strain", "by": "alice", "since": "2026-10-24T10:00:00Z", "until": None}
    (guard.tmp / "hold.json").write_text(json.dumps(hold))
    out = guard("tr-abc12", DISPATCH_FORCE="1")
    assert out.returncode == 3 and "machine-wide hold (disk strain" in out.stderr
    (guard.tmp / "hold.json").write_text("{oops")
    assert "hold file unreadable" in guard("tr-abc12").stderr


@linux
def test_an_expired_hold_or_the_override_lets_the_launch_through(guard):
    hold = {"id": "h1", "reason": "r", "by": "a", "since": "2000-01-01T00:00:00Z", "until": "2000-01-02T00:00:00Z"}
    (guard.tmp / "hold.json").write_text(json.dumps(hold))
    assert guard("tr-abc12").returncode == 0
    (guard.tmp / "hold.json").write_text("{oops")
    assert guard("tr-abc12", HERDR_HOLD_OVERRIDE="1").returncode == 0


# --- claude-worker ---

WORKER = REPO / "scripts/claude-worker"


@pytest.fixture
def worker(tmp_path):
    bin = tmp_path / "bin"
    bin.mkdir()
    (bin / "claude").write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$HOME/claude-args"\n')
    (bin / "claude").chmod(0o755)
    env = {"HOME": str(tmp_path), "PATH": f"{bin}:/usr/bin:/bin"}

    def run(*args, **extra):
        out = subprocess.run(["bash", str(WORKER), *args], capture_output=True, text=True, env={**env, **extra})
        argv = (tmp_path / "claude-args").read_text().splitlines() if (tmp_path / "claude-args").exists() else None
        return out, argv
    run.bin = bin
    return run


def test_worker_starts_claude_with_a_250k_compact_window(worker):
    out, argv = worker("--model", "m", "-w", "tree", "/hx-work tr-a")
    assert out.returncode == 0, out.stderr
    assert argv == ["--settings", '{"env":{"CLAUDE_CODE_AUTO_COMPACT_WINDOW":"250000"}}',
                    "--model", "m", "-w", "tree", "/hx-work tr-a"]


def test_worker_window_comes_from_the_env(worker):
    _, argv = worker("x", CLAUDE_CODE_AUTO_COMPACT_WINDOW="400000")
    assert argv[1] == '{"env":{"CLAUDE_CODE_AUTO_COMPACT_WINDOW":"400000"}}'


def test_worker_refuses_while_the_guard_holds_and_runs_without_a_guard(worker):
    (worker.bin / "launch-guard.sh").write_text('#!/bin/bash\necho "held: $*" >&2\nexit 3\n')
    (worker.bin / "launch-guard.sh").chmod(0o755)
    out, argv = worker("x")
    assert out.returncode == 3 and "held: --hold-only claude-worker" in out.stderr and argv is None
    (worker.bin / "launch-guard.sh").unlink()
    assert worker("x")[0].returncode == 0


def test_worker_unsets_the_overrides_after_its_guard(worker):
    (worker.bin / "launch-guard.sh").write_text('#!/bin/bash\nexit 0\n')
    (worker.bin / "launch-guard.sh").chmod(0o755)
    (worker.bin / "claude").write_text('#!/bin/bash\necho "${HERDR_HOLD_OVERRIDE-unset} ${DISPATCH_FORCE-unset}" > "$HOME/claude-args"\n')
    out, seen = worker("x", HERDR_HOLD_OVERRIDE="1", DISPATCH_FORCE="1")
    assert out.returncode == 0 and seen == ["unset unset"]


def test_worker_help_starts_nothing(worker):
    out, argv = worker("--help")
    assert out.returncode == 0 and "250000" in out.stdout and argv is None


# --- mem-gate.sh ---

def mem_gate(tmp_path, command, avail_mb=65536, **extra):
    env = {**os.environ, "MEM_GATE_MEMINFO": str(meminfo(tmp_path / "mi", avail_mb)), "MEM_GATE_MAX_PROCS": "100000", **extra}
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    out = subprocess.run(["bash", str(MEM_GATE)], input=payload, capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)["hookSpecificOutput"] if out.stdout.strip() else None


@needs_jq
@pytest.mark.parametrize("command", ["npm test", "npx jest src", "CI=1 vitest run", "cd app && npm run build",
                                     "node_modules/.bin/tsc --noEmit"])
def test_a_heavy_command_is_denied_while_memory_is_short(tmp_path, command):
    out = mem_gate(tmp_path, command, avail_mb=512)
    assert out["permissionDecision"] == "deny" and "only 512 MB available" in out["permissionDecisionReason"]


@needs_jq
@pytest.mark.parametrize("command", ["ls", "echo jest", "git commit -m 'npm test passes'"])
def test_a_light_command_or_a_mere_mention_goes_through(tmp_path, command):
    assert mem_gate(tmp_path, command, avail_mb=512) is None


@needs_jq
def test_heavy_commands_go_through_while_memory_is_plenty(tmp_path):
    assert mem_gate(tmp_path, "npm test", avail_mb=65536) is None


@needs_jq
def test_too_many_test_processes_deny_too(tmp_path):
    out = mem_gate(tmp_path, "npm test", MEM_GATE_MAX_PROCS="0")
    assert out["permissionDecision"] == "deny" and "already running" in out["permissionDecisionReason"]


@needs_jq
def test_the_pattern_and_the_off_switch(tmp_path):
    assert mem_gate(tmp_path, "pytest -q", avail_mb=512) is None
    assert mem_gate(tmp_path, "pytest -q", avail_mb=512, MEM_GATE_PATTERN="(^| )pytest( |$)")["permissionDecision"] == "deny"
    assert mem_gate(tmp_path, "npm test", avail_mb=512, MEM_GATE_OFF="1") is None


# --- checkout-freshness.sh ---

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


@pytest.fixture
def clones(tmp_path):
    origin, seed, work = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(seed))
    git(seed, "commit", "-q", "--allow-empty", "-m", "one")
    git(seed, "push", "-q", "origin", "HEAD:main")
    git(tmp_path, "clone", "-q", str(origin), str(work))
    return seed, work


def freshness(cwd, **extra):
    out = subprocess.run(["bash", str(FRESHNESS)], capture_output=True, text=True,
                         env={**os.environ, **GIT_ENV, "CLAUDE_PROJECT_DIR": str(cwd), **extra})
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"] if out.stdout.strip() else None


@needs_jq
def test_a_checkout_at_the_tip_hears_nothing(clones):
    assert freshness(clones[1]) is None


@needs_jq
def test_a_stale_checkout_is_told_how_far_behind_it_is(clones):
    seed, work = clones
    for n in range(3):
        git(seed, "commit", "-q", "--allow-empty", "-m", f"more {n}")
    git(seed, "push", "-q", "origin", "HEAD:main")
    msg = freshness(work)
    assert "CHECKOUT IS STALE" in msg and "3 commits BEHIND origin/main" in msg and "work (branch main)" in msg


@needs_jq
def test_outside_a_repo_or_without_the_base_it_stays_quiet(clones, tmp_path):
    assert freshness(tmp_path / "nowhere") is None
    plain = tmp_path / "plain"
    plain.mkdir()
    git(plain, "init", "-q")
    assert freshness(plain) is None


# --- merge_settings.py ---

def merge(settings, action, *hooks, dry=False):
    args = [sys.executable, str(MERGE), action, "--settings", str(settings)]
    for h in hooks:
        args += ["--hook", *h]
    return subprocess.run(args + (["--dry-run"] if dry else []), capture_output=True, text=True)


MEM = ("PreToolUse", "Bash", "/r/claude/hooks/mem-gate.sh")
FRESH = ("SessionStart", "", "/r/claude/hooks/checkout-freshness.sh")


def test_merge_adds_once_and_removes_only_its_own(tmp_path):
    s = tmp_path / "settings.json"
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/elsewhere/mine.sh"}]}
    s.write_text(json.dumps({"model": "x", "hooks": {"PreToolUse": [other]}}))
    assert merge(s, "add", MEM, FRESH).returncode == 0
    first = s.read_text()
    assert "already up to date" in merge(s, "add", MEM, FRESH).stdout and s.read_text() == first
    data = json.loads(first)
    assert data["hooks"]["PreToolUse"][1] == {"matcher": "Bash", "hooks": [{"type": "command", "command": MEM[2]}]}
    assert data["hooks"]["SessionStart"] == [{"hooks": [{"type": "command", "command": FRESH[2]}]}]
    assert list(tmp_path.glob("settings.json.hx-bak-*"))
    assert merge(s, "remove", MEM, FRESH).returncode == 0
    assert json.loads(s.read_text()) == {"model": "x", "hooks": {"PreToolUse": [other]}}


def test_merge_refuses_bad_json_and_dry_run_writes_nothing(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text("{oops")
    out = merge(s, "add", MEM)
    assert out.returncode != 0 and "not valid JSON" in out.stderr and s.read_text() == "{oops"
    s.write_text("")
    assert "would change" in merge(s, "add", MEM, dry=True).stdout and s.read_text() == ""


def test_merge_keeps_non_ascii_text_as_it_was(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({"notes": ["private — local only"]}, ensure_ascii=False), encoding="utf-8")
    assert merge(s, "add", MEM).returncode == 0
    assert "private — local only" in s.read_text(encoding="utf-8") and "\\u2014" not in s.read_text(encoding="utf-8")
