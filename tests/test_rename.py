"""herdr-extension became hx: the folders move once, on first use, and never leave an empty one."""
from hx.config import config_path
from hx.state import state_dir


def test_config_path_moves_the_old_folder_once(monkeypatch, tmp_path):
    monkeypatch.delenv("HX_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "herdr-extension").mkdir()
    (tmp_path / "herdr-extension/config.toml").write_text("# mine\n")
    assert config_path() == tmp_path / "hx/config.toml"
    assert (tmp_path / "hx/config.toml").read_text() == "# mine\n"
    assert not (tmp_path / "herdr-extension").exists()


def test_config_path_keeps_both_when_the_new_folder_exists(monkeypatch, tmp_path):
    monkeypatch.delenv("HX_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "herdr-extension").mkdir()
    (tmp_path / "hx").mkdir()
    assert config_path() == tmp_path / "hx/config.toml"
    assert (tmp_path / "herdr-extension").exists()


def test_config_path_moves_nothing_under_hx_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HX_CONFIG", str(tmp_path / "x.toml"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "herdr-extension").mkdir()
    assert config_path() == tmp_path / "x.toml"
    assert (tmp_path / "herdr-extension").exists() and not (tmp_path / "hx").exists()


def test_state_dir_moves_the_old_folder_once(monkeypatch, tmp_path):
    monkeypatch.delenv("HX_STATE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    old = tmp_path / ".local/state/herdr-extension"
    old.mkdir(parents=True)
    (old / "state.json").write_text("{}")
    assert state_dir() == tmp_path / ".local/state/hx"
    assert (tmp_path / ".local/state/hx/state.json").read_text() == "{}" and not old.exists()


def test_state_dir_is_made_when_neither_exists(monkeypatch, tmp_path):
    monkeypatch.delenv("HX_STATE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert state_dir().is_dir() and state_dir() == tmp_path / ".local/state/hx"


def test_state_dir_moves_nothing_under_hx_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HX_STATE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".local/state/herdr-extension").mkdir(parents=True)
    assert state_dir() == tmp_path / "s"
    assert (tmp_path / ".local/state/herdr-extension").exists()
