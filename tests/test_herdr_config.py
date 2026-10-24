"""hx sidebar setup: three keys of herdr's config.toml, every other byte kept."""
import tomllib

import pytest

from hx import herdr_config as hc

HX = "/home/u/.local/bin/hx"

LIVE = '''[keys]
prefix = "ctrl+b"

[ui]
tab_bar_right = [{ type = "command", command = "/x/herdr-projects needs-you --line", interval_seconds = 15, timeout_seconds = 5 }]

[ui.toast]
delivery = "off"

[ui.sidebar.agents]
rows = [["state_icon", "agent"], [{ token = "$hp_sub", dim = true }]]

[ui.sidebar.spaces]
rows = [["state_icon", "workspace"]]

[[keys.command]]
key = "prefix+n"
type = "plugin_action"
'''


def parsed(text):
    return tomllib.loads(text)


def test_setup_sets_the_three_keys_and_keeps_the_rest():
    new = hc.edit(LIVE, HX)
    got, old = parsed(new), parsed(LIVE)
    assert got["ui"]["sidebar_width"] == hc.WIDTH
    assert got["ui"]["sidebar"]["agents"]["rows"] == hc.ROWS
    assert got["ui"]["tab_bar_right"] == hc.tab_bar(HX)
    for key in ("sidebar_width", "tab_bar_right"):
        got["ui"].pop(key, None); old["ui"].pop(key, None)
    got["ui"]["sidebar"]["agents"].pop("rows"); old["ui"]["sidebar"]["agents"].pop("rows")
    assert got == old
    assert '[ui.toast]\ndelivery = "off"' in new and 'key = "prefix+n"' in new


def test_setup_twice_changes_nothing_more():
    once = hc.edit(LIVE, HX)
    assert hc.edit(once, HX) == once


def test_setup_adds_missing_sections():
    new = parsed(hc.edit('onboarding = false\n', HX))
    assert new["onboarding"] is False
    assert new["ui"]["sidebar"]["agents"]["rows"] == hc.ROWS and new["ui"]["sidebar_width"] == hc.WIDTH


def test_setup_refuses_a_value_over_several_lines():
    multi = LIVE.replace('rows = [["state_icon", "agent"], [{ token = "$hp_sub", dim = true }]]',
                         'rows = [\n  ["state_icon", "agent"],\n]')
    with pytest.raises(hc.ConfigError, match="by hand"):
        hc.edit(multi, HX)


def test_setup_refuses_a_config_that_does_not_parse():
    with pytest.raises(hc.ConfigError, match="does not parse"):
        hc.edit("[ui\n", HX)


def test_setup_refuses_dotted_keys_it_cannot_place():
    with pytest.raises(hc.ConfigError):
        hc.edit('ui.sidebar_width = 20\n', HX)


def test_tab_bar_names_the_hx_command():
    assert hc.tab_bar(HX) == [{"type": "command", "command": f"{HX} needs-you --line",
                               "interval_seconds": 15, "timeout_seconds": 5}]


def test_apply_backs_up_and_replaces(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(LIVE)
    backup = hc.apply(cfg, HX, stamp="20261022T000000Z")
    assert backup.read_text() == LIVE and backup.name == "config.toml.bak-hx-20261022T000000Z"
    assert parsed(cfg.read_text())["ui"]["sidebar_width"] == hc.WIDTH


def test_apply_with_nothing_to_change_writes_no_backup(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(hc.edit(LIVE, HX))
    assert hc.apply(cfg, HX, stamp="s") is None
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]


def test_apply_on_a_refused_config_changes_nothing(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[ui\n")
    with pytest.raises(hc.ConfigError):
        hc.apply(cfg, HX, stamp="s")
    assert cfg.read_text() == "[ui\n" and len(list(tmp_path.iterdir())) == 1


# --- the commands -------------------------------------------------------------------------------

from hx.cli import main  # noqa: E402
from hx.state import state_dir  # noqa: E402
from hx import sidebar  # noqa: E402


def test_needs_you_line_without_a_watcher_says_stale(capsys):
    assert main(["needs-you", "--line"]) == 0
    assert capsys.readouterr().out == "hx stale\n"


def test_needs_you_line_reads_the_watchers_file(capsys):
    import time
    sidebar.write_line(state_dir() / "sidebar.json",
                       {"p": sidebar.Row("p", "tr-1", "9m!", 4.0, 540)}, time.time())
    main(["needs-you", "--line"])
    assert capsys.readouterr().out == "⚑ 1 · tr-1 4\n"


def test_setup_dry_run_prints_the_change_and_writes_nothing(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text(LIVE)
    assert main(["sidebar", "setup", "--dry-run", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "+sidebar_width = 34" in out and "-rows = " in out
    assert cfg.read_text() == LIVE and len(list(tmp_path.iterdir())) == 1


def test_setup_writes_with_a_backup_then_has_nothing_to_do(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text(LIVE)
    assert main(["sidebar", "setup", "--config", str(cfg)]) == 0
    assert "reload-config" in capsys.readouterr().out
    assert len(list(tmp_path.glob("config.toml.bak-hx-*"))) == 1
    assert main(["sidebar", "setup", "--config", str(cfg)]) == 0
    assert "nothing to change" in capsys.readouterr().out


def test_setup_refusal_exits_1(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[ui\n")
    assert main(["sidebar", "setup", "--config", str(cfg)]) == 1
    assert "does not parse" in capsys.readouterr().err
