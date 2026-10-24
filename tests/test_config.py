import getpass
from pathlib import Path

from hx.config import MODEL, load_config


def test_defaults_when_missing(tmp_path):
    c = load_config(tmp_path / "none.toml")
    assert c.humans == {getpass.getuser(): ["console"]}
    assert c.models == {"orchestrator": MODEL, "worker": MODEL, "solo": MODEL} and MODEL == "claude-sonnet-5-5"
    assert c.close_delay_s == 600


def test_file_overrides_per_key(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[models]\norchestrator = "claude-opus-5-5"\n[spaces]\nclose_delay_s = 5\n'
                 '[humans.michael]\nchannels = ["console"]\n')
    c = load_config(p)
    assert c.models["orchestrator"] == "claude-opus-5-5" and c.models["worker"] == MODEL
    assert c.close_delay_s == 5 and c.humans == {"michael": ["console"]}


def test_solo_command_uses_claude_worker_when_installed(tmp_path, monkeypatch):
    (tmp_path / "claude-worker").write_text("#!/bin/sh\n")
    (tmp_path / "claude-worker").chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    c = load_config(tmp_path / "none.toml")
    assert c.commands["solo"] == "claude-worker --model '{model}' --effort {effort} -w {worktree} '/hx-work {root}'"


def test_solo_command_falls_back_to_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    c = load_config(tmp_path / "none.toml")
    assert c.commands["solo"] == "claude --model '{model}' --effort {effort} -w {worktree} '/hx-work {root}'"


def test_trk_contract_comes_from_the_file(tmp_path):
    assert load_config(tmp_path / "none.toml").trk_contract is None
    p = tmp_path / "c.toml"
    p.write_text('[trk]\ncontract = "~/trk/contract"\n')
    assert load_config(p).trk_contract == str(Path.home() / "trk/contract")


def test_config_path_follows_xdg(monkeypatch, tmp_path):
    from hx.config import config_path
    monkeypatch.delenv("HX_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_path() == tmp_path / "hx/config.toml"
    monkeypatch.setenv("HX_CONFIG", str(tmp_path / "x.toml"))
    assert config_path() == tmp_path / "x.toml"
