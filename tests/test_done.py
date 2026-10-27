"""Upstream ask UP-0006: a worker's finished turn wakes its driver, once per turn."""
from hx.herdr import Pane
from hx.reconcile import reconcile
from hx.tracker import Trk
from tests.conftest import FakeHerdr

WORKING = Pane("w1:p2", "w1", "s-worker", "working", "/")
FINISHED = Pane("w1:p2", "w1", "s-worker", "done", "/")
DRIVER = Pane("w2:p1", "w2", "s-driver", "idle", "/")
DRIVER_BUSY = Pane("w2:p1", "w2", "s-driver", "working", "/")


def run(trk_home, tmp_path, *panes):
    h = FakeHerdr(list(panes))
    reconcile(Trk(home=str(trk_home)), h, tmp_path / "state.json", tmp_path / "inbox.log", tmp_path / "q",
              {"michael": ["console"]})
    return [(p, t) for p, t in h.typed if p == "w2:p1"]


def driven(sh, tree):
    sh("claim", tree["program"], "--as", "orchestrator", "--session", "s-driver", "--force")


def test_a_finished_turn_wakes_the_driver_once(trk_home, sh, tree, tmp_path):
    driven(sh, tree)
    run(trk_home, tmp_path, WORKING, DRIVER)               # hx maps s-worker to its ticket as a worker
    run(trk_home, tmp_path, WORKING, DRIVER)
    lines = run(trk_home, tmp_path, FINISHED, DRIVER)
    assert lines == [("w2:p1", f"[done] id:done:s-worker:1 from:hx [hx] w1:p2 ({tree['ticket']}) finished its "
                               f"turn. Run trk show {tree['ticket']}.")]
    assert run(trk_home, tmp_path, FINISHED, DRIVER) == []                 # idle again, no new turn: nothing
    run(trk_home, tmp_path, WORKING, DRIVER)
    assert [t.split(" from:hx")[0] for _, t in run(trk_home, tmp_path, FINISHED, DRIVER)] == \
        ["[done] id:done:s-worker:2"]                                       # the next turn: one more


def test_a_busy_driver_hears_it_when_it_is_idle(trk_home, sh, tree, tmp_path):
    driven(sh, tree)
    run(trk_home, tmp_path, WORKING, DRIVER_BUSY)
    run(trk_home, tmp_path, WORKING, DRIVER_BUSY)
    assert run(trk_home, tmp_path, FINISHED, DRIVER_BUSY) == []
    assert len(run(trk_home, tmp_path, FINISHED, DRIVER)) == 1


def test_no_driver_no_line(trk_home, sh, tree, tmp_path):
    run(trk_home, tmp_path, WORKING, DRIVER)
    run(trk_home, tmp_path, WORKING, DRIVER)
    assert run(trk_home, tmp_path, FINISHED, DRIVER) == []
    inbox = tmp_path / "inbox.log"
    assert not inbox.exists() or "finished its turn" not in inbox.read_text()
