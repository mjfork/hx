from hx.config import Config, MODEL, COMMANDS
from hx.herdr import Pane, Workspace
from hx.spaces import Close, MapRow, Note, Release, Report, Snapshot, decide
from hx.state import State

CFG = Config({"michael": ["console"]}, {"orchestrator": MODEL, "worker": MODEL, "solo": MODEL}, COMMANDS, 600)
T = 1_000_000.0


def item(id, root=None, status="open", stage="built", kind="work-item", depth=1, jira=None, crit=("pass", "fail")):
    return {"id": id, "root": root, "status": status, "stage": stage, "kind": kind, "depth": depth,
            "jira_url": jira, "criteria": [{"status": s, "retired": False} for s in crit]}


def claim(it, session, actor="worker"):
    return {"item": it, "session": session, "actor": actor}


def snap(claims=(), items=(), asks=(), panes=(), workspaces=None, now=T):
    panes = list(panes)
    ws = workspaces if workspaces is not None else [Workspace(w, "") for w in sorted({p.workspace_id for p in panes})]
    return Snapshot(list(claims), {i["id"]: i for i in items}, list(asks), panes, ws, now)


ROOT = item("tr-r", depth=0, stage=None, kind="program", crit=())
TICK = item("tr-t", root="tr-r", jira="https://x.atlassian.net/browse/PROJ-9")
P_ORCH = Pane("w1:p1", "w1", "s-orch", "idle", "/", "w1:t1")
P_WORK = Pane("w1:p2", "w1", "s-work", "working", "/", "w1:t1")


def test_claim_from_a_pane_adopts_its_workspace():
    p = decide(snap([claim("tr-t", "s-work")], [ROOT, TICK], panes=[P_WORK]), State(), CFG)
    assert p.bindings["tr-r"]["workspace"] == "w1" and p.bindings["tr-r"]["origin"] == "adopted"


def test_claim_from_outside_herdr_opens_nothing():
    p = decide(snap([claim("tr-t", "s-sub")], [ROOT, TICK], panes=[P_ORCH]), State(), CFG)
    assert p.bindings == {} and not [e for e in p.effects if not isinstance(e, Report)]


def test_adopted_orchestrator_is_the_root_claimant():
    s = snap([claim("tr-r", "s-orch", "orchestrator"), claim("tr-t", "s-work")], [ROOT, TICK],
             panes=[P_ORCH, P_WORK])
    assert decide(s, State(), CFG).bindings["tr-r"]["orchestrator"] == "s-orch"


def test_adopted_space_is_never_closed_and_drops_with_its_root():
    st = State(bindings={"tr-r": {"workspace": "w1", "origin": "adopted", "orchestrator": None, "role": None,
                                  "pane": None, "tab": None, "opened_at": "x", "idle_since": None}})
    closed = dict(ROOT, status="closed")
    p = decide(snap([], [closed], panes=[P_ORCH], now=T + 10_000), st, CFG)
    assert "tr-r" not in p.bindings and not any(isinstance(e, Close) for e in p.effects)


def hx_binding(**kw):
    b = {"workspace": "w1", "origin": "hx", "orchestrator": "s-orch", "role": "orchestrator",
         "pane": "w1:p1", "tab": "w1:t1", "opened_at": "o", "idle_since": None}
    return {**b, **kw}


def test_hx_space_closes_after_delay_when_root_closed_and_all_quiet():
    closed = dict(ROOT, status="closed")
    st = State(bindings={"tr-r": hx_binding()})
    ws = [Workspace("w1", "tr-r Thing")]
    p1 = decide(snap([], [closed], panes=[P_ORCH], workspaces=ws), st, CFG)
    assert p1.bindings["tr-r"]["idle_since"] == T and not any(isinstance(e, Close) for e in p1.effects)
    st.bindings = p1.bindings
    p2 = decide(snap([], [closed], panes=[P_ORCH], workspaces=ws, now=T + 600), st, CFG)
    assert Close("close:tr-r:o", "tr-r", "w1") in p2.effects and "tr-r" not in p2.bindings


def test_busy_pane_holds_the_close():
    closed = dict(ROOT, status="closed")
    st = State(bindings={"tr-r": hx_binding(idle_since=T - 5000)})
    p = decide(snap([], [closed], panes=[P_ORCH, P_WORK]), st, CFG)
    assert p.bindings["tr-r"]["idle_since"] is None and not any(isinstance(e, Close) for e in p.effects)


def test_open_root_never_closes():
    st = State(bindings={"tr-r": hx_binding(idle_since=T - 5000)})
    p = decide(snap([], [ROOT], panes=[P_ORCH]), st, CFG)
    assert not any(isinstance(e, Close) for e in p.effects)


def test_hx_space_closed_by_hand_notes_once_and_does_not_reopen():
    st = State(bindings={"tr-r": hx_binding()})
    other = Pane("w9:p1", "w9", None, "unknown", "/")
    st.bindings = decide(snap([], [ROOT], panes=[other]), st, CFG).bindings   # missing, not yet gone
    p = decide(snap([], [ROOT], panes=[other], now=T + 60), st, CFG)
    notes = [e for e in p.effects if isinstance(e, Note)]
    assert p.bindings == {} and len(notes) == 1 and "hx start tr-r" in notes[0].text
    st.done[notes[0].key] = "t"
    st.bindings = {"tr-r": hx_binding(missing_since=T)}
    assert not [e for e in decide(snap([], [ROOT], panes=[other], now=T + 60), st, CFG).effects
                if isinstance(e, Note)]


def test_no_workspaces_reported_drops_nothing():
    st = State(bindings={"tr-r": hx_binding()})
    p = decide(snap([], [ROOT], panes=[], workspaces=[]), st, CFG)
    assert "tr-r" in p.bindings


def test_hx_binding_learns_orchestrator_from_tab_one():
    st = State(bindings={"tr-r": hx_binding(orchestrator=None)})
    assert decide(snap([], [ROOT], panes=[P_ORCH]), st, CFG).bindings["tr-r"]["orchestrator"] == "s-orch"


def test_sidebar_tokens():
    asks = [{"id": "tr-a", "root": "tr-r"}]
    p = decide(snap([claim("tr-t", "s-work")], [ROOT, TICK], asks, panes=[P_WORK]), State(), CFG)
    reps = {(e.kind, e.target): e.value for e in p.effects if isinstance(e, Report)}
    assert reps[("ws", "w1")] == "tr-r · program · 1w · ⚑1"
    assert reps[("pane", "w1:p2")] == "tr-t ac 1/2"


def test_two_roots_in_one_workspace_share_one_token():
    r2, t2 = item("tr-r2", depth=0, stage=None, kind="project", crit=()), item("tr-t2", root="tr-r2")
    p2 = Pane("w1:p3", "w1", "s-two", "idle", "/")
    p = decide(snap([claim("tr-t", "s-work"), claim("tr-t2", "s-two")], [ROOT, TICK, r2, t2],
                    panes=[P_WORK, p2]), State(), CFG)
    ws = [e.value for e in p.effects if isinstance(e, Report) and e.kind == "ws"]
    assert ws == ["tr-r · program · 1w  tr-r2 · project · 1w"]


def test_unchanged_token_is_not_re_reported_until_refresh():
    s = snap([claim("tr-t", "s-work")], [ROOT, TICK], panes=[P_WORK])
    st = State(reported={"ws:w1": ["tr-r · program · 1w", T - 10], "pane:w1:p2": ["tr-t ac 1/2", T - 10]})
    assert not [e for e in decide(s, st, CFG).effects if isinstance(e, Report)]
    st.reported["ws:w1"][1] = T - 600
    assert [e.target for e in decide(s, st, CFG).effects if isinstance(e, Report)] == ["w1"]


def test_stale_token_cleared_if_alive_else_forgotten():
    st = State(reported={"ws:w1": ["old", T], "pane:w1:p9": ["old", T]})
    p = decide(snap([], [], panes=[P_ORCH]), st, CFG)
    assert Report("report:ws:w1", "ws", "w1", None) in p.effects
    assert "pane:w1:p9" in p.forget


def test_session_gone_from_every_pane_releases_its_claims():
    st = State(seen={"s-work": "w1:p2"})
    p = decide(snap([claim("tr-t", "s-work")], [ROOT, TICK], panes=[P_ORCH]), st, CFG)
    assert [e.session for e in p.effects if isinstance(e, Release)] == ["s-work"]


def test_clear_into_new_uuid_releases_only_the_old_one():
    st = State(seen={"s-old": "w1:p2"})
    now = Pane("w1:p2", "w1", "s-new", "idle", "/")
    p = decide(snap([claim("tr-t", "s-old")], [ROOT, TICK], panes=[now]), st, CFG)
    assert [e.session for e in p.effects if isinstance(e, Release)] == ["s-old"]
    assert p.seen == {"s-new": "w1:p2"}


def test_pane_move_releases_nothing():
    st = State(seen={"s-work": "w1:p2"})
    moved = Pane("w7:p1", "w7", "s-work", "working", "/")
    p = decide(snap([claim("tr-t", "s-work")], [ROOT, TICK], panes=[moved]), st, CFG)
    assert not [e for e in p.effects if isinstance(e, Release)]


def test_no_panes_releases_nothing():
    st = State(seen={"s-work": "w1:p2"})
    p = decide(snap([claim("tr-t", "s-work")], [ROOT, TICK], panes=[], workspaces=[]), st, CFG)
    assert not [e for e in p.effects if isinstance(e, Release)] and p.seen == st.seen


def test_session_without_claims_is_not_released():
    st = State(seen={"s-x": "w1:p5"})
    assert not [e for e in decide(snap([], [], panes=[P_ORCH]), st, CFG).effects if isinstance(e, Release)]


def test_map_rows_for_hx_orchestrator_and_adopted_worker():
    st = State(bindings={"tr-r": hx_binding()})
    p = decide(snap([claim("tr-r", "s-orch", "orchestrator"), claim("tr-t", "s-work")], [ROOT, TICK],
                    panes=[P_ORCH, P_WORK]), st, CFG)
    rows = {e.session: e.row for e in p.effects if isinstance(e, MapRow)}
    assert rows["s-orch"] | {"at": None} == {"at": None, "session": "s-orch", "root": "tr-r", "item": "tr-r",
                                            "role": "orchestrator", "ticket": None, "model": MODEL, "pane": "w1:p1",
                                            "parent": None, "effort": None, "reason": None}
    assert rows["s-work"]["role"] == "worker" and rows["s-work"]["ticket"] == "PROJ-9"
    assert rows["s-work"]["model"] is None


def test_map_row_only_when_item_or_role_changes():
    st = State(bindings={"tr-r": hx_binding()}, sessions={"s-orch": ["tr-r", "orchestrator"]})
    p = decide(snap([], [ROOT], panes=[P_ORCH]), st, CFG)
    assert not [e for e in p.effects if isinstance(e, MapRow)]


def test_session_with_two_claims_maps_to_the_deepest():
    deep = item("tr-d", root="tr-r", depth=2)
    p = decide(snap([claim("tr-t", "s-work"), claim("tr-d", "s-work")], [ROOT, TICK, deep], panes=[P_WORK]),
               State(), CFG)
    assert [e.item for e in p.effects if isinstance(e, MapRow)] == ["tr-d"]


LAUNCH = {"model": "claude-opus-5-5", "effort": "high", "reason": "kickoff framing", "parent": "s-boss"}


def test_map_row_carries_launch_values_for_the_hx_session():
    st = State(bindings={"tr-r": hx_binding(role="solo", launch=LAUNCH)})
    p = decide(snap([claim("tr-r", "s-orch")], [ROOT], panes=[P_ORCH]), st, CFG)
    row = next(e.row for e in p.effects if isinstance(e, MapRow) and e.session == "s-orch")
    assert {k: row[k] for k in ("model", "effort", "reason", "parent")} == LAUNCH and row["role"] == "solo"


def test_map_row_without_launch_keeps_nulls():
    st = State(bindings={"tr-r": hx_binding(role="solo")})
    p = decide(snap([claim("tr-r", "s-orch")], [ROOT], panes=[P_ORCH]), st, CFG)
    row = next(e.row for e in p.effects if isinstance(e, MapRow) and e.session == "s-orch")
    assert (row["parent"], row["effort"], row["reason"]) == (None, None, None) and row["model"] == MODEL


def test_adopted_worker_in_an_hx_space_gets_nulls():
    st = State(bindings={"tr-r": hx_binding(role="solo", launch=LAUNCH)})
    p = decide(snap([claim("tr-r", "s-orch"), claim("tr-t", "s-work")], [ROOT, TICK],
                    panes=[P_ORCH, P_WORK]), st, CFG)
    row = next(e.row for e in p.effects if isinstance(e, MapRow) and e.session == "s-work")
    assert (row["parent"], row["effort"], row["reason"], row["model"]) == (None, None, None, None)


def test_launch_values_stay_with_the_launched_session():
    st = State(bindings={"tr-r": hx_binding(role="solo", launch=LAUNCH, orchestrator=None)})
    p1 = decide(snap([claim("tr-r", "s-orch")], [ROOT], panes=[P_ORCH]), st, CFG)
    assert p1.bindings["tr-r"]["launched"] == "s-orch"
    st.bindings, st.sessions = p1.bindings, {"s-orch": ["tr-r", "solo"]}
    other = Pane("w1:p1", "w1", "s-other", "idle", "/", "w1:t1")
    p2 = decide(snap([claim("tr-r", "s-other")], [ROOT], panes=[other]), st, CFG)
    row = next(e.row for e in p2.effects if isinstance(e, MapRow) and e.session == "s-other")
    assert (row["parent"], row["effort"], row["reason"]) == (None, None, None) and row["model"] == MODEL
