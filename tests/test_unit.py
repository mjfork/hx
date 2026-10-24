"""The systemd unit must reach the same trk, herdr and queue folder as an interactive pane."""
from pathlib import Path

UNIT = Path(__file__).parent.parent / "systemd" / "hx.service.in"   # install.sh fills in @HX@ and @BINDIR@


def env_lines():
    return [l.split("=", 1)[1] for l in UNIT.read_text().splitlines() if l.startswith("Environment=")]


def test_unit_puts_local_bin_on_path():
    # systemd's user PATH lacks ~/.local/bin, where trk, herdr and hx usually live; hx's own bin
    # directory comes first.
    assert any(e.startswith("PATH=@BINDIR@:%h/.local/bin:") for e in env_lines())


def test_unit_names_the_default_herdr_session():
    # needs-you.sh reads ~/.herdr-queue/${HERDR_SESSION:-default}; pin it so the two agree.
    assert "HERDR_SESSION=default" in env_lines()
