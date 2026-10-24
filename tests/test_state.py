from hx.state import AskInfo, State, load, save


def test_missing_file_is_a_fresh_state(tmp_path):
    s = load(tmp_path / "state.json")
    assert (s.epoch, s.seq, s.asks, s.done) == (None, 0, {}, {})


def test_round_trip(tmp_path):
    p = tmp_path / "state.json"
    s = State(epoch="e1", seq=7, asks={"tr-a": AskInfo("s1", 5, "Colour?", "Which?")},
              done={"deliver:tr-a:5:console": "2026-10-11T00:00:00Z"})
    save(s, p)
    assert load(p) == s


def test_save_leaves_no_temp_file(tmp_path):
    save(State(), tmp_path / "state.json")
    assert [f.name for f in tmp_path.iterdir()] == ["state.json"]


def test_old_state_file_loads_with_new_fields(tmp_path):
    p = tmp_path / "s.json"
    p.write_text('{"epoch": "e", "seq": 3, "asks": {}, "done": {}, "bindings": {}, "pending": [],'
                 ' "last_pass_at": null}')
    s = load(p)
    assert s.sessions == {} and s.seen == {} and s.reported == {}


def test_lock_excludes_a_second_holder(tmp_path):
    import fcntl

    import pytest

    from hx.state import locked
    p = tmp_path / "s.json"
    with locked(p):
        with open(p.with_name("s.json.lock"), "w") as f:
            with pytest.raises(BlockingIOError):
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
