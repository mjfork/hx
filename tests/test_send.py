"""hx send: type a line into panes the way the watcher wakes them, never over a person's draft."""
import pytest

from hx import drafts
from hx.cli import main
from hx.herdr import HerdrError
from hx.send import send
from tests.conftest import EMPTY_BOX, FakeHerdr, box

SHELL = "ubuntu@host:~$ \r\n"


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture(autouse=True)
def quiet_30(monkeypatch):
    monkeypatch.setattr(drafts, "QUIET_S", 30)


def go(herdr, panes, text="hello", wait=45):
    c = Clock()
    return send(herdr, panes, text, wait, clock=c, sleep=c.sleep), c


def test_an_empty_box_is_sent_after_the_quiet_window():
    h = FakeHerdr(listed(("w1:p1", "s-a")))
    out, c = go(h, ["w1:p1"])
    assert out == {"w1:p1": "sent"} and h.wakes == [("w1:p1", "hello")]
    assert 30 <= c.t < 33


def test_a_draft_is_refused_and_never_typed_over():
    h = FakeHerdr(listed(("w1:p1", "s-a"), ("w2:p1", "s-b")), screens={"w1:p1": box("half a message")})
    out, _ = go(h, ["w1:p1"])
    assert h.wakes == [] and out["w1:p1"].startswith("refused") and "text in the input box" in out["w1:p1"]


def test_a_pane_without_a_box_is_refused():
    h = FakeHerdr(listed(("w1:p1", "s-a"), ("w2:p1", "s-b")), screens={"w1:p1": SHELL})
    out, _ = go(h, ["w1:p1"])
    assert h.wakes == [] and "cannot be read" in out["w1:p1"]


def test_a_failed_read_is_refused():
    h = FakeHerdr(listed(("w1:p1", "s-a"), ("w2:p1", "s-b")), screens={"w1:p1": HerdrError("no such pane")})
    out, _ = go(h, ["w1:p1"])
    assert h.wakes == [] and "cannot be read" in out["w1:p1"]


def test_panes_are_watched_together_and_each_sent_once():
    h = FakeHerdr(listed(("w1:p1", "s-a"), ("w2:p1", "s-b")), screens={"w2:p1": box("typing")})
    out, c = go(h, ["w1:p1", "w2:p1", "w1:p1"])
    assert h.wakes == [("w1:p1", "hello")] and out["w1:p1"] == "sent" and out["w2:p1"].startswith("refused")
    assert c.t < 50                                           # one window for all, not one each


def test_a_box_emptied_midway_is_sent_once_quiet():
    screens = {"w1:p1": box("typing")}
    h = FakeHerdr(listed(("w1:p1", "s-a"), ("w2:p1", "s-b")), screens=screens)
    c = Clock()

    def sleep(s):
        c.sleep(s)
        if c.t >= 10:
            screens["w1:p1"] = EMPTY_BOX
    out = send(h, ["w1:p1"], "hello", 60, clock=c, sleep=sleep)
    assert out == {"w1:p1": "sent"} and 40 <= c.t < 43


def test_a_box_not_quiet_in_time_is_refused():
    screens = {"w1:p1": box("typing")}
    h = FakeHerdr(listed(("w1:p1", "s-a"), ("w2:p1", "s-b")), screens=screens)
    c = Clock()

    def sleep(s):
        c.sleep(s)
        if c.t >= 30:
            screens["w1:p1"] = EMPTY_BOX
    out = send(h, ["w1:p1"], "hello", 45, clock=c, sleep=sleep)
    assert h.wakes == [] and "not been empty for 30" in out["w1:p1"]


def test_the_text_is_one_line():
    h = FakeHerdr(listed(("w1:p1", "s-a")))
    go(h, ["w1:p1"], text="two\nlines  here")
    assert h.wakes == [("w1:p1", "two lines here")]


def test_a_failed_send_is_reported():
    class Broken(FakeHerdr):
        def wake(self, pane_id, text):
            raise HerdrError("pane closed")
    out, _ = go(Broken(listed(("w1:p1", "s-a"))), ["w1:p1"])
    assert out["w1:p1"] == "refused: herdr could not type into it (pane closed)"


# --- the command ---

def test_hx_send_reports_each_pane_and_exits_by_the_worst(monkeypatch, capsys):
    import hx.cli
    h = FakeHerdr(listed(("w1:p1", "s-a"), ("w2:p1", "s-b")), screens={"w2:p1": box("typing")})
    monkeypatch.setattr(hx.cli, "Herdr", lambda: h)
    monkeypatch.setattr(drafts, "QUIET_S", 0)
    assert main(["send", "--pane", "w1:p1", "hello"]) == 0
    assert "sent  w1:p1" in capsys.readouterr().out
    assert main(["send", "--pane", "w1:p1", "--pane", "w2:p1", "--wait", "1", "hi"]) == 1
    out = capsys.readouterr().out
    assert "sent  w1:p1" in out and "refused  w2:p1" in out


def test_hx_send_needs_a_pane_and_text():
    with pytest.raises(SystemExit):
        main(["send", "hello"])
    with pytest.raises(SystemExit):
        main(["send", "--pane", "w1:p1"])


def test_hx_send_refuses_a_wait_shorter_than_the_quiet_window(capsys):
    assert main(["send", "--pane", "w1:p1", "--wait", "10", "hi"]) == 2
    assert "at least 30" in capsys.readouterr().err


# --- codex review 1 ---

def test_a_new_session_in_the_pane_starts_the_quiet_window_over():
    from hx.herdr import Pane
    panes = [Pane("w1:p1", "w1", "s-old", "idle", "/")]

    class Swapping(FakeHerdr):
        def panes(self):
            return panes
    h = Swapping([])
    c = Clock()

    def sleep(s):
        c.sleep(s)
        if c.t >= 20:
            panes[0] = Pane("w1:p1", "w1", "s-new", "idle", "/")
    out = send(h, ["w1:p1"], "hello", 60, clock=c, sleep=sleep)
    assert out == {"w1:p1": "sent"} and c.t >= 50              # 30 s of quiet in the new session


def test_nothing_is_sent_after_the_deadline_even_when_a_read_runs_late():
    screens = {"w1:p1": box("typing")}

    class Slow(FakeHerdr):
        def read(self, pane_id):
            out = super().read(pane_id)
            c.t += 4                                            # each read takes 4 s
            return out
    h = Slow(listed(("w1:p1", "s-a")), screens=screens)
    c = Clock()

    def sleep(s):
        c.sleep(s)
        if c.t >= 4:
            screens["w1:p1"] = EMPTY_BOX
    out = send(h, ["w1:p1"], "hello", 30, clock=c, sleep=sleep)
    assert h.wakes == [] and out["w1:p1"].startswith("refused")


@pytest.mark.parametrize("wait", ["nan", "inf", "-5"])
def test_hx_send_refuses_a_wait_that_is_not_a_finite_number(wait):
    with pytest.raises(SystemExit):
        main(["send", "--pane", "w1:p1", "--wait", wait, "hi"])


# --- codex review 2 ---

from hx.herdr import Pane


def listed(*pairs):
    return [Pane(p, p.split(":")[0], s, "idle", "/") for p, s in pairs]


def test_a_pane_herdr_does_not_list_is_never_sent():
    h = FakeHerdr(listed(("w2:p1", "s-b")))
    out, _ = go(h, ["w1:p1"])
    assert h.wakes == [] and "pane list" in out["w1:p1"]


def test_a_failed_pane_list_sends_nothing():
    class NoList(FakeHerdr):
        def panes(self):
            raise HerdrError("down")
    h = NoList([])
    out, _ = go(h, ["w1:p1"])
    assert h.wakes == [] and "pane list" in out["w1:p1"]


def test_a_session_that_changes_just_before_typing_gets_nothing():
    calls = []

    class Turnover(FakeHerdr):
        def panes(self):
            calls.append(1)
            return listed(("w1:p1", "s-old" if len(calls) < 17 else "s-new"))
    h = Turnover([])
    out, _ = go(h, ["w1:p1"], wait=32)
    assert h.wakes == [] and out["w1:p1"].startswith("refused")


def test_the_guard_refuses_the_real_herdr_by_path():
    import shutil
    from hx.herdr import Herdr
    real = shutil.which("herdr")
    if real is None:
        pytest.skip("no herdr on this machine")
    with pytest.raises(HerdrError, match="never call live herdr"):
        Herdr(real).panes()
