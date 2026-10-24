import json

from hx.herdr import Pane
from hx.reconcile import reconcile
from hx.spaces import Release
from hx.tracker import Trk
from tests.conftest import FakeHerdr

W = Pane("w1:p2", "w1", "s-worker", "idle", "/", "w1:t1")


def go(trk_home, tmp_path, herdr, **kw):
    return reconcile(Trk(home=str(trk_home)), herdr, tmp_path / "state.json", tmp_path / "inbox.log",
                     tmp_path / "q", {"michael": ["console"]}, **kw)


def test_adopts_reports_and_maps(trk_home, tree, tmp_path):
    h = FakeHerdr([W])
    go(trk_home, tmp_path, h)
    st = json.loads((tmp_path / "state.json").read_text())
    assert st["bindings"][tree["program"]]["workspace"] == "w1"
    assert ("report_space", "w1", f"{tree['program']} · program · 1w") in h.calls
    row = json.loads((tmp_path / "sessions.jsonl").read_text().splitlines()[0])
    assert row["session"] == "s-worker" and row["item"] == tree["ticket"] and row["role"] == "worker"


def test_second_pass_is_quiet(trk_home, tree, tmp_path):
    go(trk_home, tmp_path, FakeHerdr([W]))
    h = FakeHerdr([W])
    go(trk_home, tmp_path, h)
    assert h.calls == [] and len((tmp_path / "sessions.jsonl").read_text().splitlines()) == 1


def test_session_leaving_herdr_has_its_claim_released(trk_home, sh, tree, tmp_path):
    go(trk_home, tmp_path, FakeHerdr([W]))
    other = Pane("w2:p1", "w2", None, "unknown", "/")
    acts = go(trk_home, tmp_path, FakeHerdr([other]))
    assert [a.session for a in acts if isinstance(a, Release)] == ["s-worker"]
    assert sh("claims")["claims"] == []


def test_dry_run_changes_nothing(trk_home, tree, tmp_path):
    go(trk_home, tmp_path, FakeHerdr([W]))
    before = (tmp_path / "state.json").read_text()
    h = FakeHerdr([Pane("w2:p1", "w2", None, "unknown", "/")])
    acts = go(trk_home, tmp_path, h, dry_run=True)
    assert any(isinstance(a, Release) for a in acts) and h.calls == []
    assert (tmp_path / "state.json").read_text() == before


def test_map_rows_carry_routing_fields_as_null(trk_home, tree, tmp_path):
    go(trk_home, tmp_path, FakeHerdr([W]))
    row = json.loads((tmp_path / "sessions.jsonl").read_text().splitlines()[0])
    assert {k: row[k] for k in ("parent", "effort", "reason")} == \
        {"parent": None, "effort": None, "reason": None}
