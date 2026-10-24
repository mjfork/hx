"""hx never types into a pane while a person has text in its input box, or soon after."""
import time

import pytest

from hx import drafts
from hx.drafts import draft
from hx.herdr import HerdrError
from hx.reconcile import reconcile
from hx.tracker import Trk
from tests.conftest import EMPTY_BOX, RULE, FakeHerdr, box
from tests.test_reconcile import HUMANS, WORKER, ask_michael

PLACEHOLDER = box("\x1b[0m\x1b[2mcheck CI on both PRs\x1b[0m")
TYPED = box("fix the login test\x1b[7m \x1b[0m")
SHELL = "ubuntu@host:~$ \r\n"


def run(trk_home, tmp_path, herdr, now=None, dry=False):
    return reconcile(Trk(home=str(trk_home)), herdr, tmp_path / "state.json",
                     tmp_path / "inbox.log", tmp_path / "q", HUMANS, dry_run=dry, now=now)


@pytest.fixture
def answered(trk_home, sh, tree, tmp_path):
    """An answered ask whose wake is owed to WORKER."""
    a = ask_michael(sh, tree)
    run(trk_home, tmp_path, FakeHerdr([WORKER]))
    sh("answer", a, "--outcome", "Blue", "--answer", "-", stdin="Blue")
    return a


def test_an_empty_box_has_no_draft():
    assert draft(EMPTY_BOX) == ""


def test_a_placeholder_suggestion_is_not_a_draft():
    assert draft(PLACEHOLDER) == ""


def test_typed_text_is_a_draft():
    assert draft(TYPED) == "fix the login test"


def test_a_draft_on_a_continuation_line_counts():
    assert draft(box("first line", "  second line")) == "first line second line"
    assert draft(box("", "  only the second")) == "only the second"


def test_a_truecolor_with_a_2_in_it_is_not_dim():
    assert draft(box("\x1b[38;2;2;2;2mtyped\x1b[0m")) == "typed"


def test_a_past_prompt_in_the_history_is_not_the_box():
    history = "\x1b[48;2;55;55;55m❯ \x1b[38;2;255;255;255m/reload-plugins\x1b[0m\r\n"
    assert draft(history + EMPTY_BOX) == ""


def test_a_screen_without_a_box_is_unreadable():
    assert draft(SHELL) is None
    assert draft(box("dialog").split("\r\n", 2)[1]) is None   # a ❯ line with no rule above


def test_a_box_cut_off_by_a_short_pane_runs_to_the_bottom_of_the_screen():
    cut = lambda screen: screen.rsplit("\r\n\x1b[0m\x1b[38;2;136;136;136m" + RULE, 1)[0]  # noqa: E731
    assert draft(cut(EMPTY_BOX)) == ""
    assert draft(cut(PLACEHOLDER)) == ""
    assert draft(cut(box("first line", "  second line"))) == "first line second line"


def test_a_draft_holds_the_wake_until_the_box_is_empty(trk_home, answered, tmp_path):
    held = FakeHerdr([WORKER], screens={"w1:p2": TYPED})
    run(trk_home, tmp_path, held)
    assert held.wakes == []
    sent = FakeHerdr([WORKER])
    run(trk_home, tmp_path, sent)
    assert sent.wakes == [("w1:p2", f"[hx] {answered} answered: Blue. Run trk show {answered}.")]


def test_a_placeholder_does_not_hold_the_wake(trk_home, answered, tmp_path):
    h = FakeHerdr([WORKER], screens={"w1:p2": PLACEHOLDER})
    run(trk_home, tmp_path, h)
    assert [p for p, _ in h.wakes] == ["w1:p2"]


def test_the_box_must_be_empty_for_30_seconds(trk_home, answered, tmp_path, monkeypatch):
    monkeypatch.setattr(drafts, "QUIET_S", 30)
    monkeypatch.setattr(drafts, "STALE_S", 60)      # these passes are far apart; continuity has its own test
    t0 = time.time()
    for t in (0, 10, 29):
        h = FakeHerdr([WORKER])
        run(trk_home, tmp_path, h, now=t0 + t)
        assert h.wakes == [], t
    h = FakeHerdr([WORKER])
    run(trk_home, tmp_path, h, now=t0 + 31)
    assert [p for p, _ in h.wakes] == ["w1:p2"]


def test_a_draft_restarts_the_quiet_period(trk_home, answered, tmp_path, monkeypatch):
    monkeypatch.setattr(drafts, "QUIET_S", 30)
    monkeypatch.setattr(drafts, "STALE_S", 60)      # these passes are far apart; continuity has its own test
    t0 = time.time()
    run(trk_home, tmp_path, FakeHerdr([WORKER]), now=t0)
    run(trk_home, tmp_path, FakeHerdr([WORKER], screens={"w1:p2": TYPED}), now=t0 + 20)
    h = FakeHerdr([WORKER])
    run(trk_home, tmp_path, h, now=t0 + 40)
    assert h.wakes == []
    h = FakeHerdr([WORKER])
    run(trk_home, tmp_path, h, now=t0 + 69)          # 29 s since the box was first seen empty
    assert h.wakes == []
    h = FakeHerdr([WORKER])
    run(trk_home, tmp_path, h, now=t0 + 71)
    assert [p for p, _ in h.wakes] == ["w1:p2"]


def test_an_unreadable_pane_holds_and_is_logged_once_after_10_minutes(trk_home, answered, tmp_path):
    t0 = time.time()
    inbox = tmp_path / "inbox.log"
    for t in (0, 599):
        h = FakeHerdr([WORKER], screens={"w1:p2": SHELL})
        run(trk_home, tmp_path, h, now=t0 + t)
        assert h.wakes == [] and "HELD" not in inbox.read_text()
    for t in (601, 700):
        run(trk_home, tmp_path, FakeHerdr([WORKER], screens={"w1:p2": SHELL}), now=t0 + t)
    held = [l.split("  ", 1)[1] for l in inbox.read_text().splitlines() if "  HELD  " in l]
    assert held == ["HELD  w1:p2  hx cannot see the input box of this pane for 10 minutes, so its "
                    "wakes wait. A dialog or menu is open, or the pane is too short to show the box."]
    h = FakeHerdr([WORKER])
    run(trk_home, tmp_path, h, now=t0 + 800)
    assert [p for p, _ in h.wakes] == ["w1:p2"]


def test_a_new_unreadable_spell_is_logged_again(trk_home, answered, tmp_path):
    t0 = time.time()
    for t, screen in ((0, SHELL), (601, SHELL), (610, TYPED), (620, SHELL), (1221, SHELL)):
        run(trk_home, tmp_path, FakeHerdr([WORKER], screens={"w1:p2": screen}), now=t0 + t)
    assert (tmp_path / "inbox.log").read_text().count("HELD  w1:p2") == 2


def test_a_failed_read_holds_the_wake(trk_home, answered, tmp_path):
    h = FakeHerdr([WORKER], screens={"w1:p2": HerdrError("pane read: gone")})
    run(trk_home, tmp_path, h)
    assert h.wakes == []
    h = FakeHerdr([WORKER])
    run(trk_home, tmp_path, h)
    assert [p for p, _ in h.wakes] == ["w1:p2"]


def test_a_dry_run_reads_no_screen(trk_home, answered, tmp_path):
    h = FakeHerdr([WORKER])
    run(trk_home, tmp_path, h, dry=True)
    assert h.reads == []


def test_a_gone_pane_is_forgotten(trk_home, answered, tmp_path):
    import json
    run(trk_home, tmp_path, FakeHerdr([WORKER], screens={"w1:p2": TYPED}))
    assert "w1:p2" in json.loads((tmp_path / "state.json").read_text())["drafts"]
    run(trk_home, tmp_path, FakeHerdr([]))
    assert json.loads((tmp_path / "state.json").read_text())["drafts"] == {}


def test_a_rule_with_the_session_name_on_it_still_opens_the_box():
    named = EMPTY_BOX.replace(RULE, RULE[:-11] + " director ─", 1)
    assert draft(named) == ""
    assert draft(named.replace("❯ \x1b[0m", "❯ \x1b[0mhalf a thought")) == "half a thought"


def test_colon_form_sgr_arguments_do_not_turn_on_dim():
    assert draft(box("\x1b[4:2mtyped\x1b[0m")) == "typed"
    assert draft(box("\x1b[38:2::255:255:2mtyped\x1b[0m")) == "typed"
    assert draft(box("\x1b[2mhint\x1b[22mtyped\x1b[0m")) == "typed"


def test_a_box_needs_two_full_width_rules():
    short = "───\r\n❯ \r\n───\r\nDIALOG\r\n"
    assert draft(short) is None
    narrow = "x" * 60 + "\r\n" + EMPTY_BOX          # the box's edges span the whole screen
    assert draft(narrow) is None


def test_any_read_error_holds_only_that_wake(trk_home, answered, tmp_path):
    h = FakeHerdr([WORKER], screens={"w1:p2": UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")})
    run(trk_home, tmp_path, h)
    assert h.wakes == []


def test_the_quiet_clock_restarts_after_a_gap_in_sightings(trk_home, answered, tmp_path, monkeypatch):
    monkeypatch.setattr(drafts, "QUIET_S", 30)
    t0 = time.time()
    run(trk_home, tmp_path, FakeHerdr([WORKER]), now=t0)
    h = FakeHerdr([WORKER])
    run(trk_home, tmp_path, h, now=t0 + 31)          # no sighting between 0 and 31
    assert h.wakes == []
    for t in range(32, 65, 3):                    # sightings 3 s apart; 31 s of quiet by t=62
        h = FakeHerdr([WORKER])
        run(trk_home, tmp_path, h, now=t0 + t)
    assert [p for p, _ in h.wakes] == ["w1:p2"]


def test_a_held_wake_is_saved_as_owed(trk_home, tree, tmp_path, monkeypatch):
    """The first save after reading a comment row already holds it, so a kill there loses nothing."""
    import hx.reconcile
    from tests.test_comments import FRESH, routed
    from tests.test_comments import run as run_c
    run_c(trk_home, tmp_path, FakeHerdr([WORKER]))
    saved, real = [], hx.reconcile.save
    monkeypatch.setattr(hx.reconcile, "save",
                        lambda s, p: (saved.append([r["data"]["comment_id"] for r in s.comments]), real(s, p)))
    run_c(trk_home, tmp_path, FakeHerdr([WORKER], screens={"w1:p2": TYPED}),
          [routed(tree["ticket"], tree["program"], FRESH)])
    assert saved[0] == ["c-9"]


def test_the_held_line_comes_at_exactly_10_minutes(trk_home, answered, tmp_path):
    t0 = time.time()
    for t in (0, 600):
        run(trk_home, tmp_path, FakeHerdr([WORKER], screens={"w1:p2": SHELL}), now=t0 + t)
    assert "HELD  w1:p2" in (tmp_path / "inbox.log").read_text()


def test_a_sent_wake_drops_the_pane_record(trk_home, answered, tmp_path):
    import json
    run(trk_home, tmp_path, FakeHerdr([WORKER]))
    assert json.loads((tmp_path / "state.json").read_text())["drafts"] == {}


def test_a_new_session_in_the_pane_restarts_the_quiet_clock(monkeypatch):
    from hx.state import State
    monkeypatch.setattr(drafts, "QUIET_S", 30)
    state, h = State(), FakeHerdr([WORKER])
    assert drafts.clear(h, "w1:p2", state, 100.0, "s-a") == (False, None)
    for t in range(110, 131, 10):
        drafts.clear(h, "w1:p2", state, float(t), "s-a")
    assert drafts.clear(h, "w1:p2", state, 131.0, "s-b") == (False, None)
    assert drafts.clear(h, "w1:p2", state, 131.5, "s-b")[0] is False


def test_only_a_quiet_countdown_asks_for_quick_passes():
    from hx.state import State
    state = State(drafts={"w1:p2": {"empty_since": None, "unreadable_since": 5.0, "logged": False}})
    assert not drafts.counting(state)
    state.drafts["w1:p3"] = {"empty_since": 5.0, "unreadable_since": None, "logged": False}
    assert drafts.counting(state)


def test_the_watcher_passes_every_2_seconds_while_a_pane_counts_down(tmp_path):
    from hx.cli import _tick
    from hx.state import State, save
    path = tmp_path / "state.json"
    assert _tick(path, 30.0) == 30.0
    save(State(drafts={"w1:p2": {"empty_since": 5.0, "unreadable_since": None, "logged": False}}), path)
    assert _tick(path, 30.0) == drafts.QUICK_S == 2.0
    assert drafts.STALE_S >= 3 * drafts.QUICK_S


def test_a_box_far_above_the_bottom_of_the_screen_is_not_the_box():
    footer = "\r\n".join(f"  line {i}" for i in range(11))
    assert draft(EMPTY_BOX + footer) is None
    assert draft(EMPTY_BOX + "\r\n".join(f"  line {i}" for i in range(8))) == ""


def test_rule_widths_are_compared_in_terminal_cells():
    wide = EMPTY_BOX.replace(RULE, RULE[:-5] + " 界 ─", 1)   # five cells: 界 takes two
    assert draft(wide) == ""


def test_a_menu_under_the_box_is_unreadable():
    assert draft(EMPTY_BOX + "  ❯ 1. Yes\r\n    2. No\r\n") is None
    assert draft(EMPTY_BOX + "  Enter to select · Esc to cancel\r\n") is None


def test_the_named_top_rule_is_not_measured():
    emoji = EMPTY_BOX.replace(RULE, RULE[:-5] + " 👨\u200d👩\u200d👧 ─", 1)   # 40 cells, as drawn
    assert draft(emoji) == ""


def test_a_sent_wake_drops_its_record_before_the_next_save(trk_home, answered, tmp_path, monkeypatch):
    import hx.reconcile
    saved, real = [], hx.reconcile.save
    monkeypatch.setattr(hx.reconcile, "save", lambda st, p: (saved.append(dict(st.drafts)), real(st, p)))
    h = FakeHerdr([WORKER])
    run(trk_home, tmp_path, h)
    assert h.wakes and all("w1:p2" not in d for d in saved)


def test_quick_passes_never_slow_a_faster_tick_or_wait_on_a_long_interval(tmp_path):
    from hx.cli import _sleep_for, _tick
    from hx.state import State, save
    path = tmp_path / "state.json"
    save(State(drafts={"w1:p2": {"empty_since": 5.0, "unreadable_since": None, "logged": False}}), path)
    assert _tick(path, 1.0) == 1.0
    assert _sleep_for(20.0, _tick(path, 30.0)) == 2.0
    assert _sleep_for(1.0, 30.0) == 1.0


def test_a_short_rule_inside_a_draft_does_not_open_a_new_box():
    assert draft(box("first line typed by person", "───", "❯")) == "first line typed by person ─── ❯"


def test_emoji_sequences_take_their_rendered_width():
    from hx.drafts import _cells
    assert _cells("👍🏽") == 2
    assert _cells("👨‍👩‍👧") == 2
    wide = "x" * 38 + "👍🏽" + "\r\n" + EMPTY_BOX               # 40 cells, as wide as the box
    assert draft(wide) == ""


def test_one_numbered_status_line_is_not_a_menu():
    assert draft(EMPTY_BOX + "  1. main branch\r\n") == ""
    assert draft(EMPTY_BOX + "  1. main branch\r\n  2. tests passing\r\n") == ""
    assert draft(EMPTY_BOX + "  ❯ 1. Yes\r\n    2. No\r\n") is None


def test_watch_rejects_a_tick_or_interval_that_is_not_positive(monkeypatch):
    from hx import cli
    from hx.cli import main
    monkeypatch.setattr(cli, "_watch", lambda interval, tick: 0)   # never loop in a test
    assert main(["watch"]) == 0
    for args in (["watch", "--tick", "0"], ["watch", "--interval", "-1"]):
        with pytest.raises(SystemExit):
            main(args)


def test_a_long_rule_inside_a_draft_does_not_open_a_new_box():
    for n in (20, 21, 39):
        assert draft(box("earlier text", "─" * n, "❯")) == f"earlier text {'─' * n} ❯", n


def test_a_narrow_named_edge_is_measured_in_cells():
    from hx.drafts import _rule
    assert _rule("─" * 16 + " 界")  is False      # does not end in ─
    assert _rule("─" * 15 + " 界 ─") is True       # 19 code points, 20 cells


def test_an_emoji_tag_sequence_takes_two_cells():
    from hx.drafts import _cells
    england = "\U0001F3F4\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F"
    assert _cells(england) == 2


def test_watch_rejects_a_tick_that_is_not_finite(monkeypatch):
    from hx import cli
    monkeypatch.setattr(cli, "_watch", lambda interval, tick: 0)
    for args in (["watch", "--tick", "nan"], ["watch", "--interval", "inf"]):
        with pytest.raises(SystemExit):
            cli.main(args)


def test_a_rule_in_a_draft_in_a_short_pane_does_not_open_a_new_box():
    cut = lambda screen: screen.rsplit("\r\n\x1b[0m\x1b[38;2;136;136;136m" + RULE, 1)[0]  # noqa: E731
    for n in (20, 30, 39):
        assert draft(cut(box("earlier text", "─" * n, "❯"))) == f"earlier text {'─' * n} ❯", n


def test_an_emoji_presentation_selector_makes_two_cells():
    from hx.drafts import _cells
    assert _cells("❤️") == 2
    assert _cells("❤️‍🔥") == 2


def test_a_blank_screen_is_unreadable():
    assert draft("") is None


def test_a_status_line_that_starts_with_the_prompt_sign_does_not_hide_the_draft():
    assert draft(box("private draft").replace("  ~/projects/app", "❯ ~/projects/app")) == "private draft"
    assert draft(box("private draft") + "\x1b[2m❯\x1b[0m\r\n") == "private draft"


def test_a_menu_right_under_a_full_rule_is_unreadable():
    menu = "\x1b[0m\x1b[38;2;136;136;136m" + RULE + "\x1b[0m\r\n❯ 1. Yes\r\n  2. No\r\n  Enter to select\r\n"
    assert draft("Do you want to proceed?\r\n" + menu) is None


def test_a_keycap_takes_two_cells():
    from hx.drafts import _cells
    assert _cells("1️⃣") == 2
