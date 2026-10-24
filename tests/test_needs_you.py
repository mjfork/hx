"""An answered ask also clears its pane's needs-you item: the wake starts a turn that asks again if it must."""
import json
import subprocess

import pytest

import hx.needs_you as needs_you_mod
from tests.conftest import FakeHerdr
from tests.test_reconcile import WORKER, WORKER_BUSY, ask_michael, run

REAL_CLEAR = needs_you_mod.clear


def answered(trk_home, sh, tree, tmp_path, outcome="Blue"):
    a = ask_michael(sh, tree)
    run(trk_home, tmp_path, FakeHerdr([WORKER]))
    sh("answer", a, "--outcome", outcome, "--answer", "-", stdin=outcome)
    return a


def test_an_answer_clears_the_askers_needs_you_item(trk_home, sh, tree, tmp_path, cleared):
    answered(trk_home, sh, tree, tmp_path)
    run(trk_home, tmp_path, FakeHerdr([WORKER]))
    assert cleared == ["w1:p2"]
    run(trk_home, tmp_path, FakeHerdr([WORKER]))
    assert cleared == ["w1:p2"]                              # once


def test_a_held_wake_clears_nothing_yet(trk_home, sh, tree, tmp_path, cleared):
    answered(trk_home, sh, tree, tmp_path)
    run(trk_home, tmp_path, FakeHerdr([WORKER_BUSY]))
    assert cleared == []
    run(trk_home, tmp_path, FakeHerdr([WORKER]))
    assert cleared == ["w1:p2"]


def test_a_withdrawn_ask_clears_nothing(trk_home, sh, tree, tmp_path, cleared):
    a = ask_michael(sh, tree)
    run(trk_home, tmp_path, FakeHerdr([WORKER]))
    sh("withdraw", a, "--reason", "not needed", "--as", "worker", "--session", "s-worker")
    h = FakeHerdr([WORKER])
    run(trk_home, tmp_path, h)
    assert h.wakes and cleared == []


def test_a_failed_clear_is_logged_and_the_wake_still_counts(trk_home, sh, tree, tmp_path, monkeypatch):
    def boom(pane, ask):
        raise needs_you_mod.NeedsYouError("exit 3: no such pane")
    monkeypatch.setattr(needs_you_mod, "clear", boom)
    answered(trk_home, sh, tree, tmp_path)
    h = FakeHerdr([WORKER])
    run(trk_home, tmp_path, h)
    run(trk_home, tmp_path, h)
    assert len(h.wakes) == 1
    assert "needs-you: w1:p2: exit 3: no such pane" in (tmp_path / "watch.log").read_text()


# --- finding and running herdr-jump's needs-you.sh ---

def plugins(tmp_path, entry):
    d = tmp_path / "claude/plugins"
    d.mkdir(parents=True)
    (d / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": entry}))
    return tmp_path / "claude"


def test_the_script_comes_from_the_installed_plugin(tmp_path, monkeypatch):
    install = tmp_path / "cache/herdr-jump/0.17.0"
    (install / "bin").mkdir(parents=True)
    (install / "bin/needs-you.sh").write_text("#!/bin/sh\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(plugins(tmp_path, {"herdr-jump@mjfork": [{"installPath": str(install)}]})))
    assert needs_you_mod.script() == install / "bin/needs-you.sh"


@pytest.mark.parametrize("entry", [{}, {"herdr-jump@mjfork": []}, {"herdr-jump@mjfork": [{"installPath": "/nowhere"}]}])
def test_no_plugin_means_no_script(tmp_path, monkeypatch, entry):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(plugins(tmp_path, entry)))
    assert needs_you_mod.script() is None


def test_an_unreadable_registry_means_no_script(tmp_path, monkeypatch):
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins/installed_plugins.json").write_text("{oops")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert needs_you_mod.script() is None


def test_clear_runs_the_script_with_the_pane(tmp_path, monkeypatch):
    install = tmp_path / "hj"
    (install / "bin").mkdir(parents=True)
    log = tmp_path / "args"
    (install / "bin/needs-you.sh").write_text(f'#!/bin/bash\necho "$*" > {log}\n')
    (install / "bin/needs-you.sh").chmod(0o755)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(plugins(tmp_path, {"herdr-jump@mjfork": [{"installPath": str(install)}]})))
    REAL_CLEAR("w1:p2", "tr-abc12", queue=queue(tmp_path, "w1:p2", "Ask: ok? Reply with trk answer tr-abc12"))
    assert log.read_text() == "clear w1:p2 answered\n"


def test_clear_without_the_plugin_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(plugins(tmp_path, {})))
    REAL_CLEAR("w1:p2", "tr-abc12", queue=queue(tmp_path, "w1:p2", "tr-abc12"))   # no error, nothing run


def test_a_failing_script_raises(tmp_path, monkeypatch):
    install = tmp_path / "hj"
    (install / "bin").mkdir(parents=True)
    (install / "bin/needs-you.sh").write_text("#!/bin/bash\necho nope >&2\nexit 3\n")
    (install / "bin/needs-you.sh").chmod(0o755)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(plugins(tmp_path, {"herdr-jump@mjfork": [{"installPath": str(install)}]})))
    with pytest.raises(needs_you_mod.NeedsYouError, match="nope"):
        REAL_CLEAR("w1:p2", "tr-abc12", queue=queue(tmp_path, "w1:p2", "tr-abc12"))


# --- codex review 1 ---

def test_a_wake_that_falls_back_to_another_pane_clears_nothing():
    from hx.herdr import Pane
    from hx.routing import Wake, route
    from tests.test_routing import answered_row, st
    s = st()
    s.bindings["tr-p"] = {"orchestrator": "s-orch"}
    orch = Pane("w5:p1", "w5", "s-orch", "idle", "/")
    wake = next(a for a in route([], [answered_row()], [], {}, s, [orch], {}) if isinstance(a, Wake))
    assert wake.pane == "w5:p1" and wake.needs_you is None


def test_the_clear_runs_only_after_the_wake_is_saved(trk_home, sh, tree, tmp_path, monkeypatch):
    seen = []

    def clear(pane, ask):
        state = json.loads((tmp_path / "state.json").read_text())
        seen.append(any(k.startswith("answered:") for k in state["done"]))
    monkeypatch.setattr(needs_you_mod, "clear", clear)
    answered(trk_home, sh, tree, tmp_path)
    run(trk_home, tmp_path, FakeHerdr([WORKER]))
    assert seen == [True]                    # a crash after the clear cannot send, and clear, twice


def test_the_script_uses_herdrs_default_server(tmp_path, monkeypatch):
    install = tmp_path / "hj"
    (install / "bin").mkdir(parents=True)
    log = tmp_path / "env"
    (install / "bin/needs-you.sh").write_text(f'#!/bin/bash\necho "$HERDR_SESSION" > {log}\n')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(plugins(tmp_path, {"herdr-jump@mjfork": [{"installPath": str(install)}]})))
    monkeypatch.setenv("HERDR_SESSION", "team")
    REAL_CLEAR("w1:p2", "tr-abc12", queue=queue(tmp_path, "w1:p2", "tr-abc12"))
    assert log.read_text() == "default\n"


def queue(tmp_path, pane, text):
    q = tmp_path / "queue"
    q.mkdir(exist_ok=True)
    (q / f"{pane.replace(':', '_')}.json").write_text(json.dumps({"pane": pane, "ask": text, "ts": 1}))
    return q


def fake_script(tmp_path, monkeypatch):
    install = tmp_path / "hj"
    (install / "bin").mkdir(parents=True)
    log = tmp_path / "ran"
    (install / "bin/needs-you.sh").write_text(f'#!/bin/bash\necho "$*" >> {log}\n')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(plugins(tmp_path, {"herdr-jump@mjfork": [{"installPath": str(install)}]})))
    return log


def test_an_item_about_another_ask_is_kept(tmp_path, monkeypatch):
    log = fake_script(tmp_path, monkeypatch)
    REAL_CLEAR("w1:p2", "tr-abc12", queue=queue(tmp_path, "w1:p2", "Ask: pick a colour for tr-zzz99"))
    assert not log.exists()


def test_a_pane_with_no_item_runs_nothing(tmp_path, monkeypatch):
    log = fake_script(tmp_path, monkeypatch)
    (tmp_path / "queue").mkdir()
    REAL_CLEAR("w1:p2", "tr-abc12", queue=tmp_path / "queue")
    assert not log.exists()


def test_the_wake_names_the_answered_ask(trk_home, sh, tree, tmp_path, monkeypatch):
    got = []
    monkeypatch.setattr(needs_you_mod, "clear", lambda pane, ask: got.append((pane, ask)))
    a = answered(trk_home, sh, tree, tmp_path)
    run(trk_home, tmp_path, FakeHerdr([WORKER]))
    assert got == [("w1:p2", a)]
