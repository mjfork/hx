"""Tests for the final review's Important findings (each written before its fix)."""
import json

import pytest

from hx.channels import Channels
from hx.feed import advance
from hx.herdr import Pane
from hx.reconcile import reconcile
from hx.routing import Notify, route, still_pending
from hx.state import AskInfo, State
from hx.tracker import Trk
from tests.conftest import FakeHerdr

WORKER = Pane("w1:p2", "w1", "s-worker", "done", "/")
HUMANS = {"michael": ["console", "queue"]}


def run(trk_home, tmp_path, herdr):
    return reconcile(Trk(home=str(trk_home)), herdr, tmp_path / "state.json",
                     tmp_path / "inbox.log", tmp_path / "q", HUMANS)


def ask_michael(sh, tree):
    return sh("ask", tree["ticket"], "Colour?", "--to", "product", "--question", "-",
              "--as", "worker", "--session", "s-worker", stdin="Which colour?")["item"]["id"]


# Finding 1: a hard stop mid-pass (SIGTERM, Ctrl-C) must not lose what was already done.
class InterruptedWake(FakeHerdr):
    def wake(self, pane_id, text):
        raise KeyboardInterrupt


def test_hard_stop_mid_pass_keeps_what_was_done(trk_home, sh, tree, tmp_path):
    a = ask_michael(sh, tree)
    run(trk_home, tmp_path, FakeHerdr([WORKER]))
    sh("answer", a, "--outcome", "Blue", "--answer", "-", stdin="Blue")
    with pytest.raises(KeyboardInterrupt):
        run(trk_home, tmp_path, InterruptedWake([WORKER]))
    h = FakeHerdr([WORKER])
    run(trk_home, tmp_path, h)
    assert [p for p, _ in h.wakes] == ["w1:p2"]
    assert (tmp_path / "inbox.log").read_text().count(f"CLEARED  {a}") == 1


# Finding 2a: queue items of asks that are no longer open are swept every pass.
def test_queue_items_of_closed_asks_are_swept(tmp_path):
    c = Channels(tmp_path / "inbox.log", tmp_path / "q", {"w1:p2": "w1"})
    c.notify(Notify("k1", "michael", "queue", "tr-a", "t", "w1:p2"))
    c.notify(Notify("k2", "michael", "queue", "tr-b", "t", "w1:p2"))
    c.sweep({"tr-b"})
    assert sorted(f.name for f in (tmp_path / "q").iterdir()) == ["hx_tr-b.json"]


# Finding 2b: after a restore, rows written after the last good pass are live, not history.
def test_rebuild_treats_only_rows_before_last_pass_as_history():
    s = State(epoch="e2", seq=0, asks={})
    rows = [
        {"seq": 1, "at": "2026-10-11T10:00:00.000Z", "change": "ask.opened", "item": "tr-a",
         "session": "s-worker", "title": "t", "data": {"question": "q"}},
        {"seq": 2, "at": "2026-10-11T12:00:00.000Z", "change": "ask.answered", "item": "tr-a",
         "session": None, "data": {"outcome": "Blue"}},
    ]
    reply = {"schema": 1, "epoch": "e2", "head": 2, "next": 2, "changes": rows}
    r = advance(s, reply, replay=False, history_before="2026-10-11T11:00:00Z")
    assert [row["seq"] for row in r.closed] == [2]
    assert r.closed[0]["asker_session"] == "s-worker"


# Finding 3: closed-row keys carry the epoch, so a reused seq after a restore is not "done".
def test_closed_keys_carry_the_epoch():
    row = {"seq": 12, "change": "ask.answered", "item": "tr-a", "root": "tr-p",
           "asker_session": "s-worker", "data": {"outcome": "Blue", "parent_claim_session": None,
                                                   "parent_claim_live": False}}
    s = State(epoch="e2", seq=9, asks={"tr-a": AskInfo("s-worker", 5, "t", "q")},
              done={"clear:tr-a:12": "old", "answered:tr-a:12": "old",
                    "clear:e1:tr-a:12": "old", "answered:e1:tr-a:12": "old"})
    keys = [a.key for a in route([], [row], [], {}, s, [WORKER], {})]
    assert keys == ["clear:e2:tr-a:12", "answered:e2:tr-a:12"]
    assert still_pending([row], s, [WORKER]) == [row]


# Finding 4: a queue item lost when its pane died is written again on a live pane.
def test_lost_queue_item_is_rewritten(trk_home, sh, tree, tmp_path):
    a = ask_michael(sh, tree)
    run(trk_home, tmp_path, FakeHerdr([WORKER]))
    (tmp_path / "q" / f"hx_{a}.json").unlink()          # needs-you.sh dropped it
    run(trk_home, tmp_path, FakeHerdr([WORKER]))
    assert json.loads((tmp_path / "q" / f"hx_{a}.json").read_text())["pane"] == "w1:p2"
    assert (tmp_path / "inbox.log").read_text().count(f"ASK  {a}") == 1


def test_existing_queue_item_is_not_rewritten(tmp_path):
    c = Channels(tmp_path / "inbox.log", tmp_path / "q", {"w1:p2": "w1", "w1:p3": "w1"})
    c.notify(Notify("k", "michael", "queue", "tr-a", "t", "w1:p2"))
    c.notify(Notify("k", "michael", "queue", "tr-a", "t", "w1:p3"))
    assert json.loads((tmp_path / "q" / "hx_tr-a.json").read_text())["pane"] == "w1:p2"


# Finding 5: any error in the watch loop is logged; a contract or schema break alerts once.
def test_watch_error_logs_and_alerts_once(tmp_path):
    from hx.cli import handle_error
    from hx.tracker import TrkError
    log, inbox = tmp_path / "watch.log", tmp_path / "inbox.log"
    alerted = handle_error(TrkError("trk changes: reply breaks contract changes: 2 != 1"),
                           log, inbox, alerted=False)
    alerted = handle_error(TrkError("trk changes: reply breaks contract changes: 2 != 1"),
                           log, inbox, alerted=alerted)
    alerted2 = handle_error(KeyError("pane_id"), log, inbox, alerted=False)
    assert alerted is True and alerted2 is False
    assert log.read_text().count("ERROR") == 3
    assert inbox.read_text().count("ALERT") == 1
