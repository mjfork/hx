"""Slice 2 final-review findings, each pinned by a test that failed before its fix."""
import json

from hx.herdr import HerdrError, Pane, Workspace
from hx.reconcile import reconcile
from hx.spaces import Close, MapRow, Note, Release, Report, decide
from hx.state import State
from hx.tracker import Trk, TrkError
from tests.conftest import FakeHerdr
from tests.test_spaces import CFG, P_ORCH, P_WORK, ROOT, T, TICK, claim, hx_binding, item, snap


def kinds(plan, cls):
    return [e for e in plan.effects if isinstance(e, cls)]


# C1: a pane that is still there but shows no session does not release at once.
def test_pane_without_session_waits_before_release():
    st = State(seen={"s-work": "w1:p2"})
    blank = Pane("w1:p2", "w1", None, "unknown", "/")
    p1 = decide(snap([claim("tr-t", "s-work")], [ROOT, TICK], panes=[blank]), st, CFG)
    assert not kinds(p1, Release) and p1.seen == {"s-work": "w1:p2"} and p1.absent == {"s-work": T}
    st.seen, st.absent = p1.seen, p1.absent
    p2 = decide(snap([claim("tr-t", "s-work")], [ROOT, TICK], panes=[blank], now=T + 120), st, CFG)
    assert [e.session for e in kinds(p2, Release)] == ["s-work"]


def test_session_back_in_its_pane_forgets_the_absence():
    st = State(seen={"s-work": "w1:p2"}, absent={"s-work": T - 100})
    p = decide(snap([claim("tr-t", "s-work")], [ROOT, TICK], panes=[P_WORK]), st, CFG)
    assert not kinds(p, Release) and p.absent == {}


# I1: closing a Space does not also clear tokens on the workspace it closes.
def test_close_forgets_tokens_instead_of_clearing():
    closed = dict(ROOT, status="closed")
    st = State(bindings={"tr-r": hx_binding(idle_since=T - 600)},
               reported={"ws:w1": ["x", T], "pane:w1:p1": ["y", T]})
    p = decide(snap([], [closed], panes=[P_ORCH], workspaces=[Workspace("w1", "tr-r Thing")]), st, CFG)
    assert kinds(p, Close) and not kinds(p, Report)
    assert set(p.forget) >= {"ws:w1", "pane:w1:p1"}


# I3: a workspace holding a claimant of another open item, or bound to another root, is not closed.
def test_close_held_while_a_pane_holds_a_claim():
    closed = dict(ROOT, status="closed")
    other = item("tr-q", depth=0, kind="project", crit=())
    st = State(bindings={"tr-r": hx_binding(idle_since=T - 600)})
    q = Pane("w1:p3", "w1", "s-q", "idle", "/")
    p = decide(snap([claim("tr-q", "s-q")], [closed, other], panes=[P_ORCH, q],
                    workspaces=[Workspace("w1", "tr-r Thing")]), st, CFG)
    assert not kinds(p, Close)


# I4: a workspace whose label is not this root's is never closed by hx.
def test_close_refused_when_label_is_not_the_roots():
    closed = dict(ROOT, status="closed")
    st = State(bindings={"tr-r": hx_binding(idle_since=T - 600)})
    p = decide(snap([], [closed], panes=[P_ORCH], workspaces=[Workspace("w1", "someone else")]), st, CFG)
    assert not kinds(p, Close) and "tr-r" not in p.bindings
    assert any("not closing" in n.text for n in kinds(p, Note))


def test_herdr_calls_pin_the_default_server(tmp_path):
    from hx.herdr import Herdr
    log = tmp_path / "args.log"
    b = tmp_path / "fake-herdr"
    b.write_text(f'#!/bin/sh\necho "$@" >> {log}\necho \'{{"result": {{"workspaces": []}}}}\'\n')
    b.chmod(0o755)
    Herdr(str(b)).workspaces()
    assert log.read_text().split()[:4] == ["--session", "default", "workspace", "list"]


# I5: an hx Space follows the session now in tab 1.
def test_hx_orchestrator_follows_tab_one():
    st = State(bindings={"tr-r": hx_binding(orchestrator="s-old")})
    p = decide(snap([], [ROOT], panes=[P_ORCH]), st, CFG)
    assert p.bindings["tr-r"]["orchestrator"] == "s-orch"


# I7: a workspace missing from one list is not gone yet.
def test_missing_workspace_is_gone_only_after_a_minute():
    st = State(bindings={"tr-r": hx_binding()})
    elsewhere = Pane("w9:p1", "w9", None, "unknown", "/")
    p1 = decide(snap([], [ROOT], panes=[elsewhere]), st, CFG)
    assert "tr-r" in p1.bindings and not kinds(p1, Note)
    st.bindings = p1.bindings
    p2 = decide(snap([], [ROOT], panes=[elsewhere], now=T + 60), st, CFG)
    assert "tr-r" not in p2.bindings and kinds(p2, Note)


def test_workspace_back_clears_the_missing_clock():
    st = State(bindings={"tr-r": hx_binding(missing_since=T - 30)})
    p = decide(snap([], [ROOT], panes=[P_ORCH]), st, CFG)
    assert p.bindings["tr-r"].get("missing_since") is None


# I8: every claimant in herdr is mapped and labelled, not only those in the bound workspace.
def test_claimant_in_another_workspace_is_mapped():
    a = Pane("w1:p2", "w1", "s-a", "idle", "/")
    b = Pane("w2:p1", "w2", "s-b", "idle", "/")
    t2 = item("tr-t2", root="tr-r")
    p = decide(snap([claim("tr-t", "s-a"), claim("tr-t2", "s-b")], [ROOT, TICK, t2], panes=[a, b]), State(), CFG)
    assert {e.session for e in kinds(p, MapRow)} == {"s-a", "s-b"}
    assert {e.target for e in kinds(p, Report) if e.kind == "pane"} == {"w1:p2", "w2:p1"}


# Review Focus 1: no panes at all drops no binding and starts no close clock.
def test_no_panes_keeps_adopted_bindings_and_clocks():
    closed = dict(ROOT, status="closed")
    st = State(bindings={"tr-r": hx_binding(), "tr-q": {**hx_binding(), "origin": "adopted"}})
    p = decide(snap([], [closed, item("tr-q", depth=0, crit=())], panes=[], workspaces=[]), st, CFG)
    assert set(p.bindings) == {"tr-r", "tr-q"} and p.bindings["tr-r"]["idle_since"] is None


# I2, I6 and M5 run through reconcile.
W = Pane("w1:p2", "w1", "s-worker", "idle", "/", "w1:t1")


def go(trk_home, tmp_path, herdr):
    return reconcile(Trk(home=str(trk_home)), herdr, tmp_path / "state.json", tmp_path / "inbox.log",
                     tmp_path / "q", {"michael": ["console"]})


class FailingReports(FakeHerdr):
    def report_space(self, workspace_id, value):
        raise HerdrError("herdr says no")


def test_a_failing_effect_does_not_stop_the_others(trk_home, tree, tmp_path):
    go(trk_home, tmp_path, FailingReports([W]))
    st = json.loads((tmp_path / "state.json").read_text())
    assert tree["program"] in st["bindings"]
    assert (tmp_path / "sessions.jsonl").exists()
    assert "herdr says no" in (tmp_path / "watch.log").read_text()


def test_space_failure_does_not_block_ask_routing(trk_home, sh, tree, tmp_path, monkeypatch):
    sh("ask", tree["ticket"], "Colour?", "--to", "product", "--question", "-",
       "--as", "worker", "--session", "s-worker", stdin="Which?")
    monkeypatch.setattr(FakeHerdr, "workspaces", lambda self: (_ for _ in ()).throw(HerdrError("down")))
    go(trk_home, tmp_path, FakeHerdr([W]))
    assert "ASK" in (tmp_path / "inbox.log").read_text()
    assert "down" in (tmp_path / "watch.log").read_text()


def test_release_leaves_a_line(trk_home, tree, tmp_path):
    go(trk_home, tmp_path, FakeHerdr([W]))
    go(trk_home, tmp_path, FakeHerdr([Pane("w2:p1", "w2", None, "unknown", "/")]))
    assert "RELEASED  s-worker" in (tmp_path / "inbox.log").read_text()


# C2: a state file written by a newer hx still loads, so a rollback keeps routing asks.
def test_unknown_state_keys_are_ignored(tmp_path):
    from hx.state import load
    p = tmp_path / "s.json"
    p.write_text('{"epoch": "e", "seq": 3, "asks": {}, "done": {}, "bindings": {}, "pending": [],'
                 ' "last_pass_at": null, "from_the_future": 1}')
    assert load(p).seq == 3
