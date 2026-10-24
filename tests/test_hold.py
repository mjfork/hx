"""A machine-wide hold: one file every launcher reads, and one message to each live agent."""
import json
from dataclasses import replace

import pytest

from hx import hold
from hx.cli import main
from hx.herdr import Pane
from hx.reconcile import reconcile
from hx.tracker import Trk
from tests.conftest import FakeHerdr, box
from tests.test_reconcile import HUMANS

NOW = 1_800_000_000.0          # 2027-01-15T08:00:00Z
A = Pane("w1:p1", "w1", "s-a", "idle", "/tmp")
B = Pane("w2:p1", "w2", "s-b", "done", "/tmp")
BUSY = Pane("w3:p1", "w3", "s-c", "working", "/tmp")
SHELL = Pane("w4:p1", "w4", None, "idle", "/tmp")


def write(path, **over):
    rec = {"id": "ab12cd34", "reason": "demo", "by": "director", "since": "2027-01-15T07:00:00Z",
           "until": None, **over}
    path.write_text(json.dumps(rec))
    return rec


# --- the file ---

def test_a_missing_file_is_free(tmp_path):
    assert hold.read(tmp_path / "hold.json", NOW).kind == "free"


def test_put_writes_the_contract_and_reads_back_held(tmp_path):
    p = tmp_path / "hold.json"
    rec = hold.put(p, "disk strain", "director", None, NOW)
    raw = json.loads(p.read_text())
    assert raw == rec and set(raw) == {"id", "reason", "by", "since", "until"}
    assert raw["since"] == "2027-01-15T08:00:00Z" and raw["until"] is None and len(raw["id"]) == 8
    st = hold.read(p, NOW)
    assert st.kind == "held" and st.record == rec
    assert not list(tmp_path.glob("*.tmp"))                                # no temp file left


def test_each_hold_gets_its_own_id(tmp_path):
    p = tmp_path / "hold.json"
    assert hold.put(p, "x", "me", None, NOW)["id"] != hold.put(p, "x", "me", None, NOW)["id"]


def test_until_in_the_future_is_held_and_in_the_past_is_free(tmp_path):
    p = tmp_path / "hold.json"
    write(p, until="2027-01-15T09:00:00Z")
    assert hold.read(p, NOW).kind == "held"
    write(p, until="2027-01-15T07:59:59Z")
    assert hold.read(p, NOW).kind == "free"


@pytest.mark.parametrize("text", ["{not json", "[]", '"held"', json.dumps({"reason": "x"}),
                                  json.dumps({"id": "a", "reason": "x", "by": "b", "since": "s", "until": 5}),
                                  json.dumps({"id": "a", "reason": "x", "by": "b", "since": "s", "until": "soon"})])
def test_a_file_that_cannot_be_read_counts_as_held(tmp_path, text):
    p = tmp_path / "hold.json"
    p.write_text(text)
    st = hold.read(p, NOW)
    assert st.kind == "unreadable" and st.error


def test_resume_removes_the_file(tmp_path):
    p = tmp_path / "hold.json"
    rec = write(p)
    assert hold.resume(p) == rec and not p.exists()
    assert hold.resume(p) is None


def test_resume_removes_an_unreadable_file(tmp_path):
    p = tmp_path / "hold.json"
    p.write_text("{oops")
    assert hold.resume(p) == {} and not p.exists()


def test_expire_removes_only_an_expired_hold(tmp_path):
    p = tmp_path / "hold.json"
    write(p, until="2027-01-15T09:00:00Z")
    assert hold.expire(p, NOW) is None and p.exists()
    write(p, until="2027-01-15T07:00:00Z")
    assert hold.expire(p, NOW)["id"] == "ab12cd34" and not p.exists()
    write(p)
    assert hold.expire(p, NOW) is None and p.exists()          # until null never expires


@pytest.mark.parametrize("text,at", [("2h", NOW + 7200), ("30m", NOW + 1800), ("90s", NOW + 90),
                                     ("1d", NOW + 86400), ("2027-01-15T10:00:00Z", NOW + 7200),
                                     ("2027-01-15T10:00:00+00:00", NOW + 7200)])
def test_until_takes_a_duration_or_a_time(text, at):
    assert hold.parse_until(text, NOW) == at


@pytest.mark.parametrize("text", ["soon", "-2h", "0m", "2027-01-15T07:00:00Z", ""])
def test_until_must_be_a_later_time(text):
    with pytest.raises(ValueError):
        hold.parse_until(text, NOW)


# --- the commands ---

def state_file(name="hold.json"):
    from hx.state import state_dir
    return state_dir() / name


def test_hx_hold_then_held_then_resume(capsys):
    assert main(["hold", "--reason", "demo  for\nthe board", "--by", "director"]) == 0
    rec = json.loads(state_file().read_text())
    assert rec["reason"] == "demo for the board" and rec["by"] == "director"
    assert rec["id"] in capsys.readouterr().out
    assert main(["held"]) == 0
    assert "demo for the board" in capsys.readouterr().out
    assert main(["resume"]) == 0 and not state_file().exists()
    assert main(["held"]) == 1
    assert main(["resume"]) == 0          # nothing to resume is not an error
    assert "no hold" in capsys.readouterr().out


def test_hx_held_reports_an_unreadable_file(capsys):
    state_file().parent.mkdir(parents=True, exist_ok=True)
    state_file().write_text("{oops")
    assert main(["held"]) == 2
    assert "cannot be read" in capsys.readouterr().out


def test_hx_held_json(capsys):
    main(["hold", "--reason", "demo", "--until", "2h"])
    capsys.readouterr()
    assert main(["held", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["held"] is True and out["record"]["reason"] == "demo" and out["record"]["until"]


def test_hx_hold_refuses_a_bad_until(capsys):
    assert main(["hold", "--reason", "x", "--until", "soon"]) == 2
    assert not state_file().exists()


def test_hx_hold_needs_a_reason():
    with pytest.raises(SystemExit):
        main(["hold"])


# --- the watcher ---

def run(trk_home, tmp_path, herdr, now=NOW):
    return reconcile(Trk(home=str(trk_home)), herdr, tmp_path / "state.json",
                     tmp_path / "inbox.log", tmp_path / "q", HUMANS, now=now)


def inbox(tmp_path):
    p = tmp_path / "inbox.log"
    return p.read_text() if p.exists() else ""


def test_a_hold_tells_each_live_agent_once(trk_home, tmp_path):
    write(tmp_path / "hold.json", reason="disk strain")
    herdr = FakeHerdr([A, B, SHELL])
    run(trk_home, tmp_path, herdr)
    assert sorted(p for p, _ in herdr.wakes) == ["w1:p1", "w2:p1"]          # never the shell pane
    text = herdr.wakes[0][1]
    assert text.startswith("HOLD") and "disk strain" in text and "director" in text and "\n" not in text
    assert "HOLD  ab12cd34" in inbox(tmp_path)
    run(trk_home, tmp_path, herdr)
    assert len(herdr.wakes) == 2


def test_a_busy_or_drafting_agent_hears_the_hold_later(trk_home, tmp_path):
    write(tmp_path / "hold.json")
    herdr = FakeHerdr([A, BUSY], screens={"w1:p1": box("half a message")})
    run(trk_home, tmp_path, herdr)
    assert herdr.wakes == []
    herdr2 = FakeHerdr([A, replace(BUSY, status="idle")])
    run(trk_home, tmp_path, herdr2)
    assert sorted(p for p, _ in herdr2.wakes) == ["w1:p1", "w3:p1"]


def test_resume_tells_the_agents_that_heard_the_hold(trk_home, tmp_path):
    write(tmp_path / "hold.json")
    run(trk_home, tmp_path, FakeHerdr([A]))
    (tmp_path / "hold.json").unlink()
    herdr = FakeHerdr([A, B])
    run(trk_home, tmp_path, herdr)
    assert [p for p, _ in herdr.wakes] == ["w1:p1"]                        # B never heard the hold
    assert herdr.wakes[0][1].startswith("RESUME")
    assert "RESUMED  ab12cd34" in inbox(tmp_path)
    run(trk_home, tmp_path, herdr)
    assert len(herdr.wakes) == 1


def test_an_expired_hold_is_removed_and_resumed(trk_home, tmp_path):
    write(tmp_path / "hold.json", until="2027-01-15T08:30:00Z")
    run(trk_home, tmp_path, FakeHerdr([A]))
    herdr = FakeHerdr([A])
    run(trk_home, tmp_path, herdr, now=NOW + 3600)
    assert not (tmp_path / "hold.json").exists()
    assert [t[:6] for _, t in herdr.wakes] == ["RESUME"]


def test_a_new_hold_is_told_again(trk_home, tmp_path):
    write(tmp_path / "hold.json")
    herdr = FakeHerdr([A])
    run(trk_home, tmp_path, herdr)
    write(tmp_path / "hold.json", id="ffff0000", reason="swap strain")
    run(trk_home, tmp_path, herdr)
    assert len(herdr.wakes) == 2 and "swap strain" in herdr.wakes[1][1]


def test_an_unreadable_hold_is_logged_once_and_tells_nobody(trk_home, tmp_path):
    (tmp_path / "hold.json").write_text("{oops")
    herdr = FakeHerdr([A])
    run(trk_home, tmp_path, herdr)
    run(trk_home, tmp_path, herdr)
    assert herdr.wakes == []
    assert inbox(tmp_path).count("cannot be read") == 1


def test_the_session_start_hook_names_the_hold(tmp_path):
    import os
    import subprocess
    from pathlib import Path
    script = Path(__file__).parent.parent / "claude/hooks/hold-notice.sh"
    env = {**os.environ, "HX_STATE_DIR": str(tmp_path)}
    run_hook = lambda: subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)  # noqa: E731
    out = run_hook()
    assert out.returncode == 0 and out.stdout == ""
    write(tmp_path / "hold.json", reason="disk strain")
    out = run_hook()
    assert out.returncode == 0 and "HOLD" in out.stdout and "disk strain" in out.stdout
    write(tmp_path / "hold.json", until="2000-01-01T00:00:00Z")
    assert run_hook().stdout == ""
    (tmp_path / "hold.json").write_text("{oops")
    out = run_hook()
    assert out.returncode == 0 and "cannot be read" in out.stdout


def test_hx_start_refuses_while_held(capsys, monkeypatch):
    import hx.cli
    monkeypatch.setattr(hx.cli, "start", lambda *a, **k: pytest.fail("started during a hold"))
    main(["hold", "--reason", "disk strain"])
    assert main(["start", "tr-x"]) == 1
    assert "held" in capsys.readouterr().err
    state_file().write_text("{oops")
    assert main(["start", "tr-x"]) == 1


# --- codex review 1 ---

@pytest.mark.parametrize("until", ["2027-01-15T10:00:00", "2027-01-15", "2027-01-15 10:00:00Z",
                                   "2026-10-22T10:00:00"])
def test_a_time_without_a_zone_is_unreadable_not_local(tmp_path, until):
    p = tmp_path / "hold.json"
    write(p, until=until)
    assert hold.read(p, NOW).kind == "unreadable"


def test_until_given_without_a_zone_is_refused():
    with pytest.raises(ValueError, match="zone"):
        hold.parse_until("2027-01-15T10:00:00", NOW)


def test_a_fractional_until_is_kept_later_than_now(tmp_path):
    p = tmp_path / "hold.json"
    rec = hold.put(p, "x", "me", NOW + 0.4, NOW + 0.1)
    assert hold.read(p, NOW + 0.3).kind == "held" and rec["until"] == "2027-01-15T08:00:01Z"
    write(p, until="2027-01-15T08:00:00.500Z")
    assert hold.read(p, NOW + 0.1).kind == "held" and hold.read(p, NOW + 0.6).kind == "free"


def test_a_session_told_by_the_hook_hears_the_resume(trk_home, tmp_path):
    write(tmp_path / "hold.json")
    run(trk_home, tmp_path, FakeHerdr([BUSY]))                 # busy all hold: hx never told it
    (tmp_path / "hold.json").unlink()
    herdr = FakeHerdr([replace(BUSY, status="idle")])
    run(trk_home, tmp_path, herdr)
    assert [(p, t[:6]) for p, t in herdr.wakes] == [("w3:p1", "RESUME")]


def test_a_pane_reused_by_a_new_session_hears_no_resume_for_it(trk_home, tmp_path):
    write(tmp_path / "hold.json")
    run(trk_home, tmp_path, FakeHerdr([A]))
    (tmp_path / "hold.json").unlink()
    herdr = FakeHerdr([replace(A, session="s-new")])
    run(trk_home, tmp_path, herdr)
    assert herdr.wakes == []


def test_a_session_away_at_the_resume_hears_it_when_back(trk_home, tmp_path):
    write(tmp_path / "hold.json")
    run(trk_home, tmp_path, FakeHerdr([A]))
    (tmp_path / "hold.json").unlink()
    run(trk_home, tmp_path, FakeHerdr([]))
    herdr = FakeHerdr([replace(A, pane_id="w9:p1")])            # same session, another pane
    run(trk_home, tmp_path, herdr, now=NOW + 60)
    assert [(p, t[:6]) for p, t in herdr.wakes] == [("w9:p1", "RESUME")]


def test_resume_debt_ends_after_an_hour(trk_home, tmp_path):
    write(tmp_path / "hold.json")
    run(trk_home, tmp_path, FakeHerdr([A]))
    (tmp_path / "hold.json").unlink()
    run(trk_home, tmp_path, FakeHerdr([]))
    herdr = FakeHerdr([A])
    run(trk_home, tmp_path, herdr, now=NOW + hold.RESUME_KEEP_S + 1)
    assert herdr.wakes == []


def hook(tmp_path):
    import os
    import subprocess
    from pathlib import Path
    script = Path(__file__).parent.parent / "claude/hooks/hold-notice.sh"
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=15,
                          env={**os.environ, "HX_STATE_DIR": str(tmp_path)})


@pytest.mark.parametrize("rec", [{"until": "2000-01-01T00:00:00Z"},
                                 {"id": "a", "reason": "r", "by": "b", "since": "s", "until": "2000-01-01T00:00:00"}])
def test_the_hook_reads_by_the_same_rules_as_hx(tmp_path, rec):
    (tmp_path / "hold.json").write_text(json.dumps(rec))
    out = hook(tmp_path)
    assert out.returncode == 0 and "cannot be read" in out.stdout


def test_the_hook_never_waits_on_a_fifo(tmp_path):
    import os
    os.mkfifo(tmp_path / "hold.json")
    out = hook(tmp_path)
    assert out.returncode == 0 and "cannot be read" in out.stdout


# --- codex review 2 ---

def test_a_session_seen_while_the_file_is_unreadable_hears_the_resume(trk_home, tmp_path):
    write(tmp_path / "hold.json")
    run(trk_home, tmp_path, FakeHerdr([A]))
    (tmp_path / "hold.json").write_text("{oops")
    run(trk_home, tmp_path, FakeHerdr([A, B]))                  # B starts; the hook told it
    (tmp_path / "hold.json").unlink()
    herdr = FakeHerdr([A, B])
    run(trk_home, tmp_path, herdr)
    assert sorted(p for p, t in herdr.wakes if t.startswith("RESUME")) == ["w1:p1", "w2:p1"]


def test_text_that_is_not_utf8_is_unreadable(tmp_path):
    p = tmp_path / "hold.json"
    p.write_bytes(b'{"id": "\xff"}')
    assert hold.read(p, NOW).kind == "unreadable"


def test_hx_never_waits_on_a_fifo(tmp_path):
    import os
    import threading
    p = tmp_path / "hold.json"
    os.mkfifo(p)
    got = []
    t = threading.Thread(target=lambda: got.append(hold.read(p, NOW)), daemon=True)
    t.start()
    t.join(5)
    assert got and got[0].kind == "unreadable"


def test_a_dangling_link_is_unreadable(tmp_path):
    (tmp_path / "hold.json").symlink_to(tmp_path / "nowhere")
    assert hold.read(tmp_path / "hold.json", NOW).kind == "unreadable"


def test_an_oversized_file_is_unreadable_to_hx_and_the_hook(tmp_path):
    p = tmp_path / "hold.json"
    p.write_text(json.dumps({"id": "a", "reason": "r", "by": "b", "since": "s",
                             "until": "2000-01-01T00:00:00Z"}) + " " * hold.MAX_BYTES + "x")
    assert hold.read(p, NOW).kind == "unreadable"
    assert "cannot be read" in hook(tmp_path).stdout


# --- codex review 3 ---

def test_a_hold_unreadable_from_the_start_still_resumes_its_sessions(trk_home, tmp_path):
    (tmp_path / "hold.json").write_text("{oops")
    run(trk_home, tmp_path, FakeHerdr([A]))
    (tmp_path / "hold.json").unlink()
    herdr = FakeHerdr([A])
    run(trk_home, tmp_path, herdr)
    assert [(p, t[:6]) for p, t in herdr.wakes] == [("w1:p1", "RESUME")]
    assert "RESUMED" in inbox(tmp_path)


def test_short_reads_are_read_to_the_end(tmp_path, monkeypatch):
    import os
    p = tmp_path / "hold.json"
    p.write_text(json.dumps({"id": "a", "reason": "r", "by": "b", "since": "s",
                             "until": "2000-01-01T00:00:00Z"}) + " " * hold.MAX_BYTES + "x")
    real = os.read
    monkeypatch.setattr(os, "read", lambda fd, n: real(fd, min(n, 100)))
    assert hold.read(p, NOW).kind == "unreadable"


def test_the_hook_counts_a_file_it_cannot_reach_as_held(tmp_path):
    import os
    locked = tmp_path / "locked"
    locked.mkdir()
    write(locked / "hold.json", until="2000-01-01T00:00:00Z")
    os.chmod(locked, 0)
    try:
        out = hook(locked)
    finally:
        os.chmod(locked, 0o755)
    assert "cannot be read" in out.stdout


def test_resume_removes_an_empty_folder_and_reports_a_full_one(tmp_path, capsys):
    p = tmp_path / "hold.json"
    p.mkdir()
    assert hold.resume(p, NOW) == {} and not p.exists()
    p.mkdir()
    (p / "x").write_text("")
    with pytest.raises(OSError):
        hold.resume(p, NOW)


def test_hx_resume_reports_what_it_cannot_remove(capsys):
    state_file().mkdir(parents=True)
    (state_file() / "x").write_text("")
    assert main(["resume"]) == 2
    assert "cannot remove" in capsys.readouterr().err
