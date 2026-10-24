import json

from hx.channels import Channels
from hx.routing import Clear, Notify


def ch(tmp_path):
    return Channels(tmp_path / "inbox.log", tmp_path / "q", {"w1:p2": "w1"})


def test_console_appends_a_line(tmp_path):
    assert ch(tmp_path).notify(Notify("k", "michael", "console", "tr-a", "tr-a Colour?: Which?", None))
    assert "ASK  tr-a  michael  tr-a Colour?: Which?" in (tmp_path / "inbox.log").read_text()


def test_queue_item_has_the_needs_you_shape(tmp_path):
    assert ch(tmp_path).notify(Notify("k", "michael", "queue", "tr-a", "tr-a Colour?: Which?", "w1:p2"))
    item = json.loads((tmp_path / "q" / "hx_tr-a.json").read_text())
    assert item["pane"] == "w1:p2" and item["workspace"] == "w1"
    assert item["ask"].startswith("**Ask:** tr-a Colour?: Which?")
    assert "trk answer tr-a" in item["ask"]


def test_queue_without_pane_is_not_done(tmp_path):
    assert ch(tmp_path).notify(Notify("k", "michael", "queue", "tr-a", "t", None)) is False
    assert not (tmp_path / "q").exists() or not list((tmp_path / "q").iterdir())


def test_clear_removes_queue_item_and_logs(tmp_path):
    c = ch(tmp_path)
    c.notify(Notify("k", "michael", "queue", "tr-a", "t", "w1:p2"))
    c.clear(Clear("c", "tr-a"))
    assert not (tmp_path / "q" / "hx_tr-a.json").exists()
    assert "CLEARED  tr-a" in (tmp_path / "inbox.log").read_text()
