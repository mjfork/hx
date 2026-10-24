import pytest

from hx.feed import SchemaChanged, advance, is_quiet
from hx.state import AskInfo, State


def reply(rows, epoch="e1", head=None, schema=1):
    head = head if head is not None else (rows[-1]["seq"] if rows else 0)
    return {"schema": schema, "epoch": epoch, "head": head, "next": head, "changes": rows}


def opened(seq, ask="tr-a", session="s-worker"):
    return {"seq": seq, "change": "ask.opened", "item": ask, "session": session,
            "title": "Colour?", "data": {"question": "Which colour?"}}


def answered(seq, ask="tr-a"):
    return {"seq": seq, "change": "ask.answered", "item": ask, "session": None,
            "data": {"outcome": "Blue", "parent_claim_session": "s-worker",
                     "parent_claim_live": True}}


def test_first_poll_stores_epoch_without_rebuild():
    s = State()
    r = advance(s, reply([opened(3)]))
    assert (r.rebuilt, s.epoch, s.seq) == (False, "e1", 3)
    assert s.asks["tr-a"] == AskInfo("s-worker", 3, "Colour?", "Which colour?")


def test_answer_moves_ask_to_closed():
    s = State(epoch="e1", seq=3, asks={"tr-a": AskInfo("s-worker", 3, "Colour?", "Which?")})
    r = advance(s, reply([answered(4)]))
    assert [row["seq"] for row in r.closed] == [4]
    assert r.closed[0]["asker_session"] == "s-worker"
    assert s.asks == {}


def test_readdress_bumps_generation():
    s = State(epoch="e1", seq=3, asks={"tr-a": AskInfo("s-worker", 3, "Colour?", "Which?")})
    advance(s, reply([{"seq": 9, "change": "ask.readdressed", "item": "tr-a", "data": {}}]))
    assert s.asks["tr-a"].gen == 9


def test_new_epoch_rebuilds_but_keeps_done_keys():
    s = State(epoch="e1", seq=50, asks={"tr-a": AskInfo("s", 3, "t", "q")},
              done={"deliver:tr-a:3:console": "t0"})
    r = advance(s, reply([], epoch="e2", head=40))
    assert r.rebuilt and s.epoch == "e2" and s.seq == 0 and s.asks == {}
    assert "deliver:tr-a:3:console" in s.done


def test_lower_head_rebuilds_same_epoch():
    s = State(epoch="e1", seq=50)
    assert advance(s, reply([], head=40)).rebuilt


def test_schema_change_stops():
    with pytest.raises(SchemaChanged):
        advance(State(), reply([], schema=2))


def test_replay_from_zero_wakes_nobody():
    s = State()
    r = advance(s, reply([opened(3), answered(4)]))
    assert r.closed == [] and s.asks == {} and s.seq == 4


def test_migration_rows_are_quiet():
    assert is_quiet({"data": {"via": "migration"}})
    assert not is_quiet({"data": {"via": "cli"}})
    assert not is_quiet({"data": None})


def reopened(seq, ask="tr-a", session="s-human"):
    return {"seq": seq, "change": "ask.reopened", "item": ask, "session": session,
            "title": "Colour?", "data": {"question": "Which colour, really?"}}


def test_reopen_by_someone_else_keeps_the_original_asker():
    s = State(epoch="e1", seq=2)
    advance(s, reply([opened(3), answered(4), reopened(5)]))
    assert s.asks["tr-a"].session == "s-worker"
    assert s.asks["tr-a"].question == "Which colour, really?"


def test_reopen_by_a_person_with_no_session_keeps_the_original_asker():
    s = State(epoch="e1", seq=2)
    advance(s, reply([opened(3)]))
    advance(s, reply([answered(4)]))
    advance(s, reply([reopened(5, session=None)]))
    assert s.asks["tr-a"].session == "s-worker"


def test_answer_after_a_reopen_wakes_the_original_asker():
    s = State(epoch="e1", seq=2)
    advance(s, reply([opened(3), answered(4), reopened(5)]))
    r = advance(s, reply([answered(6)]))
    assert r.closed[0]["asker_session"] == "s-worker"


def test_reopen_of_an_ask_hx_never_saw_open_uses_the_row_session():
    s = State(epoch="e1", seq=4)
    advance(s, reply([reopened(5)]))
    assert s.asks["tr-a"].session == "s-human"


def test_an_ask_open_before_the_upgrade_keeps_its_asker_through_a_reopen():
    s = State(epoch="e1", seq=3, asks={"tr-a": AskInfo("s-worker", 3, "Colour?", "Which?")})
    advance(s, reply([answered(4), reopened(5)]))
    assert s.asks["tr-a"].session == "s-worker"


def test_rebuild_forgets_askers():
    s = State(epoch="e1", seq=2)
    advance(s, reply([opened(3)]))
    advance(s, reply([], epoch="e2"))
    assert s.askers == {}


def test_an_ask_opened_with_no_session_stays_without_one_after_a_reopen():
    s = State(epoch="e1", seq=2)
    advance(s, reply([opened(3, session=None), answered(4), reopened(5, session="s-human")]))
    assert s.asks["tr-a"].session is None


def test_a_legacy_asker_fallback_is_the_same_however_the_feed_is_batched():
    rows = [reopened(5, session="s-one"), answered(6), reopened(7, session="s-two")]
    whole = State(epoch="e1", seq=4)
    advance(whole, reply(rows))
    split = State(epoch="e1", seq=4)
    for r in rows:
        advance(split, reply([r]))
    assert whole.asks["tr-a"].session == split.asks["tr-a"].session == "s-one"
