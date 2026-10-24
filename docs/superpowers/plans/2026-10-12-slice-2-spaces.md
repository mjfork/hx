# Slice 2: Spaces, sidebar and session map — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** the watcher knows which Space belongs to which root, opens one on `hx start`, closes the ones it opened when their root is done, labels Spaces and panes in the sidebar, releases the claims of sessions that left herdr, and writes the session map the cost scoreboard reads.

**Architecture:** a new pure unit `spaces.decide(snapshot, state, cfg) -> Plan` returns the next bindings plus effects (`Report`, `Close`, `Release`, `MapRow`, `Note`). `reconcile()` gathers the snapshot after ask routing, acts on each effect, and records it in state. `hx start` opens a Space directly. A file lock keeps `hx start` and the watcher from overwriting each other's state.

**Tech Stack:** Python 3.12+, uv, pytest, the `herdr` and `trk` CLIs.

**Spec:** `docs/superpowers/specs/2026-10-11-herdr-extension-design.md` — Section 2, and "Amendments of 2026-10-12" points 2–4.

## Global Constraints

- Plugin writes to trk only `claim release` and `sweep`, as actor `herdr` (ADR 0002). This slice uses only `claim release --as herdr --session S`.
- Tests never touch the live trk store: use the `trk_home` fixture. Tests never touch the live herdr: use `FakeHerdr` or a fake `herdr` script.
- Never close a workspace hx did not open (`origin == "hx"`).
- Never close a Space while any of its panes is `working` or `blocked`.
- Close delay 10 minutes (`close_delay_s = 600`).
- Default model for every role: `claude-sonnet-5-5`.
- `sessions.jsonl` row fields exactly: `at, session, root, item, role, ticket, model, pane`. Append only.
- Sessions are matched by Claude session uuid, never by pane id.

## Review Focus

1. herdr shows no panes (restarting, or the call returns an empty list): no claim may be released and no binding dropped.
2. A session `/clear`s into a new uuid in the same pane: the old uuid's claims are released, the new uuid is not.
3. A pane moves (new pane id, same session): nothing is released.
4. Michael closes an hx Space by hand: the binding goes, a console note says how to reopen, nothing reopens on its own.
5. Two roots claimed from one workspace (the tracker's own workspace): one sidebar token holds both, and that workspace is never closed.

## Rulings made while writing this plan (spec deviations)

- **No automatic Space opening in slice 2.** Spec: "the watcher sees a live claim … and the root has no Space yet" opens one. Today most claims come from sessions already in a herdr pane (adopted, below) or from subagents that are never in a pane; opening an orchestrator Space for a subagent's claim would start an unwanted Opus/Sonnet session. So: a claim from a session in a pane *adopts* that pane's workspace as the root's Space; a claim from a session not in herdr opens nothing. `hx start` is the only opener. `auto_open` stays out until slice 4 adds the restart cap. Cost if wrong: a root claimed from outside herdr has no Space until Michael runs `hx start`.
- **No automatic reopen of a vanished Space.** Spec: after a herdr restart the watcher reopens Spaces. A workspace closed by hand looks the same. Slice 2 drops the binding and writes one console note with the `hx start` command; reopen moves to slice 4 with the restart cap. Cost if wrong: one command after a herdr restart.
- **The launched command is a config template.** The `hx-orchestrate` and `hx-work` skills ship in slice 3. Until then `hx start` launches the template as configured; the default names the slice 3 skills.

---

### Task 1: herdr adapter — workspaces, create, rename, report, close

**Files:** Modify `src/hx/herdr.py`, `tests/conftest.py`. Test `tests/test_herdr.py`.

**Interfaces — Produces:**
- `Pane(pane_id, workspace_id, session, status, cwd, tab_id="")`
- `Workspace(workspace_id: str, label: str)`; `parse_workspaces(reply) -> list[Workspace]`
- `Created(workspace_id, tab_id, pane_id)`; `parse_created(reply) -> Created`
- `Herdr.workspaces()`, `.create_space(label, cwd, env: dict) -> Created`, `.rename_tab(tab_id, label)`, `.run(pane_id, command)`, `.report_space(ws, value|None)`, `.report_pane(pane, value|None)`, `.close_space(ws)`
- `FakeHerdr(panes, workspaces=None)` records `calls: list[tuple]`.

- [ ] **Step 1: failing tests** (`tests/test_herdr.py`, appended)

```python
from hx.herdr import Created, Workspace, parse_created, parse_workspaces, Herdr

CREATED = {"result": {"root_pane": {"pane_id": "wCV:p1", "tab_id": "wCV:t1", "workspace_id": "wCV"},
                      "type": "workspace_created"}}


def test_parse_workspaces():
    reply = {"result": {"workspaces": [{"workspace_id": "w1", "label": "A"}, {"workspace_id": "w2"}]}}
    assert parse_workspaces(reply) == [Workspace("w1", "A"), Workspace("w2", "")]


def test_parse_created():
    assert parse_created(CREATED) == Created("wCV", "wCV:t1", "wCV:p1")


def test_pane_carries_tab():
    reply = {"result": {"panes": [{"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1"}]}}
    assert parse_panes(reply)[0].tab_id == "w1:t1"


def fake_bin(tmp_path, reply="{}"):
    log = tmp_path / "args.log"
    b = tmp_path / "fake-herdr"
    b.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\ncat <<'EOF'\n{reply}\nEOF\n")
    b.chmod(0o755)
    return str(b), log


def test_create_space_passes_label_cwd_env_no_focus(tmp_path):
    import json
    b, log = fake_bin(tmp_path, json.dumps(CREATED))
    c = Herdr(b).create_space("tr-1 Thing", "/repo", {"HX_ROLE": "solo"})
    assert c == Created("wCV", "wCV:t1", "wCV:p1")
    assert log.read_text().split() == ["workspace", "create", "--no-focus", "--label", "tr-1", "Thing",
                                       "--cwd", "/repo", "--env", "HX_ROLE=solo"]


def test_report_and_clear_tokens(tmp_path):
    b, log = fake_bin(tmp_path)
    h = Herdr(b)
    h.report_space("w1", "tr-1 · built · 1w")
    h.report_space("w1", None)
    h.report_pane("w1:p2", None)
    lines = log.read_text().splitlines()
    assert lines[0] == "workspace report-metadata --source hx --token hx=tr-1 · built · 1w w1"
    assert lines[1] == "workspace report-metadata --source hx --clear-token hx w1"
    assert lines[2] == "pane report-metadata --source hx --clear-token hx_sub w1:p2"
```

- [ ] **Step 2: run** `pytest tests/test_herdr.py -q` — Expected: FAIL, ImportError on `Created`.

- [ ] **Step 3: implement** in `src/hx/herdr.py`

```python
@dataclass(frozen=True)
class Pane:
    pane_id: str
    workspace_id: str
    session: str | None
    status: str
    cwd: str
    tab_id: str = ""
    ...


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    label: str


@dataclass(frozen=True)
class Created:
    workspace_id: str
    tab_id: str
    pane_id: str


def parse_panes(reply):  # adds tab_id=p.get("tab_id", "")
def parse_workspaces(reply): return [Workspace(w["workspace_id"], w.get("label", "")) for w in reply["result"]["workspaces"]]
def parse_created(reply): p = reply["result"]["root_pane"]; return Created(p["workspace_id"], p["tab_id"], p["pane_id"])

class Herdr:
    def workspaces(self): return parse_workspaces(json.loads(self._run("workspace", "list")))
    def create_space(self, label, cwd, env):
        args = ["workspace", "create", "--no-focus", "--label", label, "--cwd", cwd]
        for k, v in env.items(): args += ["--env", f"{k}={v}"]
        return parse_created(json.loads(self._run(*args)))
    def rename_tab(self, tab_id, label): self._run("tab", "rename", tab_id, label)
    def run(self, pane_id, command): self._run("pane", "run", pane_id, command)
    def report_space(self, ws, value): self._report("workspace", ws, "hx", value)
    def report_pane(self, pane, value): self._report("pane", pane, "hx_sub", value)
    def _report(self, what, target, token, value):
        tok = ["--token", f"{token}={value}"] if value is not None else ["--clear-token", token]
        self._run(what, "report-metadata", "--source", "hx", *tok, target)
    def close_space(self, ws): self._run("workspace", "close", ws)
```

`FakeHerdr` (conftest): add `workspaces` (defaults to one `Workspace` per distinct pane workspace), `calls` list, and the same methods recording `(name, *args)`; `create_space` returns `Created("wNEW", "wNEW:t1", "wNEW:p1")`.

- [ ] **Step 4: run** `pytest -q` — Expected: all pass.
- [ ] **Step 5: commit** `herdr: workspaces, create, rename, report tokens, close`

### Task 2: config — models, commands, close delay

**Files:** Modify `src/hx/config.py`. Test `tests/test_config.py`.

**Produces:** `Config(humans, models, commands, close_delay_s=600)`, `load_config(path) -> Config`, `MODEL = "claude-sonnet-5-5"`.

- [ ] **Step 1: failing tests**

```python
from hx.config import Config, load_config, MODEL


def test_defaults_when_missing(tmp_path):
    c = load_config(tmp_path / "none.toml")
    assert c.humans == {"michael": ["console", "queue"]}
    assert c.models == {"orchestrator": MODEL, "worker": MODEL, "solo": MODEL} and MODEL == "claude-sonnet-5-5"
    assert c.commands["solo"] == "claude --model {model} '/hx-work {root}'"
    assert c.close_delay_s == 600


def test_file_overrides_per_key(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[models]\norchestrator = "claude-opus-5-5"\n[spaces]\nclose_delay_s = 5\n'
                 '[humans.michael]\nchannels = ["console"]\n')
    c = load_config(p)
    assert c.models["orchestrator"] == "claude-opus-5-5" and c.models["worker"] == MODEL
    assert c.close_delay_s == 5 and c.humans == {"michael": ["console"]}
```

- [ ] **Step 2: run** — FAIL (ImportError).
- [ ] **Step 3: implement**

```python
MODEL = "claude-sonnet-5-5"
COMMANDS = {"orchestrator": "claude --model {model} '/hx-orchestrate {root}'",
            "solo": "claude --model {model} '/hx-work {root}'"}


@dataclass(frozen=True)
class Config:
    humans: dict
    models: dict
    commands: dict
    close_delay_s: int = 600


def load_config(path: Path) -> Config:
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    return Config(humans=load_humans(path),
                  models={r: raw.get("models", {}).get(r, MODEL) for r in ("orchestrator", "worker", "solo")},
                  commands={**COMMANDS, **raw.get("commands", {})},
                  close_delay_s=int(raw.get("spaces", {}).get("close_delay_s", 600)))
```

- [ ] **Step 4: run** `pytest -q` — pass. **Step 5: commit** `config: models, commands, close delay`

### Task 3: state — new fields and a lock

**Files:** Modify `src/hx/state.py`. Test `tests/test_state.py`.

**Produces:** `State.sessions: dict[str, list]` (session → [item, role] last written), `State.seen: dict[str, str]` (session → pane on the last pass), `State.reported: dict[str, list]` ("ws:<id>"/"pane:<id>" → [value, epoch s]); `locked(path)` context manager.

- [ ] **Step 1: failing tests**

```python
def test_old_state_file_loads_with_new_fields(tmp_path):
    p = tmp_path / "s.json"
    p.write_text('{"epoch": "e", "seq": 3, "asks": {}, "done": {}, "bindings": {}, "pending": [], "last_pass_at": null}')
    s = load(p)
    assert s.sessions == {} and s.seen == {} and s.reported == {}


def test_lock_excludes_a_second_holder(tmp_path):
    import fcntl
    p = tmp_path / "s.json"
    with locked(p):
        with open(p.with_name("s.json.lock"), "w") as f:
            with pytest.raises(BlockingIOError):
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
```

- [ ] **Step 2: run** — FAIL. **Step 3: implement** the three fields with `field(default_factory=dict)` and

```python
@contextmanager
def locked(path: Path):
    with open(path.with_name(path.name + ".lock"), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
```

- [ ] **Step 4–5:** `pytest -q` pass; commit `state: sessions, seen, reported, and a file lock`

### Task 4: spaces.decide — the pure Space rules

**Files:** Create `src/hx/spaces.py`. Test `tests/test_spaces.py`.

**Consumes:** `Pane`, `Workspace` (Task 1), `Config` (Task 2), `State` (Task 3).
**Produces:** `Report(key, kind, target, value)`, `Close(key, root, workspace)`, `Release(key, session)`, `MapRow(key, session, item, role, row)`, `Note(key, text)`, `Snapshot(claims, items, asks, panes, workspaces, now)`, `Plan(bindings, effects, seen, forget)`, `decide(snap, state, cfg) -> Plan`, `iso(t)`, `root_of(item)`, `ticket_of(url)`.

Rules (spec Section 2, rulings above):
1. If herdr shows workspaces, a binding whose workspace is gone is dropped; for `origin == "hx"` a `Note` keyed `gone:<root>:<opened_at>` (once) says `hx start <root>` reopens it.
2. A root with a live claim and no binding adopts the workspace of its first claimant that is in a pane (the root's orchestrator claimant first): `origin "adopted"`.
3. An adopted binding is dropped when its root closes or no claim under it comes from a pane in its workspace. Its `orchestrator` is the session holding the root's claim as actor `orchestrator`, if in that workspace.
4. An hx binding learns `orchestrator` from its tab-1 pane's session. When its root is closed and no pane in the workspace is `working` or `blocked`, `idle_since` starts; after `close_delay_s` a `Close` (once per `close:<root>:<opened_at>`) and the binding goes. Anything busy, or the root open, resets `idle_since`.
5. Sidebar: per bound workspace one `hx` token, roots joined by two spaces: `<root> · <stage or kind> · <N>w` plus ` · ⚑<asks>` when asks > 0, or `<root> · closed`. Per claimant pane in a bound workspace an `hx_sub` token `<item> ac <met>/<live>` (just `<item>` with no live criteria). Report when the value changed or the last report is ≥ 600 s old. A previously reported target with no wanted value is cleared if alive, else forgotten.
6. Release: only when herdr shows at least one pane. A session in `state.seen` that is in no pane now and holds a claim gets `Release`. `Plan.seen` is the current session → pane map.
7. Session map: per session in a bound workspace, one current (item, role): the hx orchestrator/solo session with its configured model; every other claimant session in that workspace with role `orchestrator` for actor orchestrator, else `worker`, model null; a session with several claims takes its deepest item. A `MapRow` when `state.sessions[session] != [item, role]`.

- [ ] **Step 1: failing tests** (`tests/test_spaces.py`)

```python
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
    p1 = decide(snap([], [closed], panes=[P_ORCH]), st, CFG)
    assert p1.bindings["tr-r"]["idle_since"] == T and not any(isinstance(e, Close) for e in p1.effects)
    st.bindings = p1.bindings
    p2 = decide(snap([], [closed], panes=[P_ORCH], now=T + 600), st, CFG)
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
    p = decide(snap([], [ROOT], panes=[other]), st, CFG)
    notes = [e for e in p.effects if isinstance(e, Note)]
    assert p.bindings == {} and len(notes) == 1 and "hx start tr-r" in notes[0].text
    st.done[notes[0].key] = "t"
    assert not [e for e in decide(snap([], [ROOT], panes=[other]), st, CFG).effects if isinstance(e, Note)]


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
                                            "role": "orchestrator", "ticket": None, "model": MODEL, "pane": "w1:p1"}
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
```

- [ ] **Step 2: run** `pytest tests/test_spaces.py -q` — Expected: FAIL (ModuleNotFoundError).
- [ ] **Step 3: implement** `src/hx/spaces.py` to the seven rules above.
- [ ] **Step 4: run** `pytest -q` — all pass.
- [ ] **Step 5: commit** `spaces: pure rules for adopt, close, sidebar, release and session map`

### Task 5: reconcile acts on the Space plan

**Files:** Modify `src/hx/reconcile.py`, `src/hx/tracker.py`, `src/hx/cli.py`. Test `tests/test_reconcile_spaces.py`.

**Consumes:** `decide`, effects (Task 4); `Herdr` methods (Task 1); `Config` (Task 2); `locked` (Task 3).
**Produces:** `Trk.release(session)`; `reconcile(..., cfg: Config | None = None, now: float | None = None)` returns routing actions + space effects; `sessions.jsonl` beside the state file.

- [ ] **Step 1: failing tests**

```python
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
```

- [ ] **Step 2: run** — FAIL (no binding / no `calls`).
- [ ] **Step 3: implement.**
  - `Trk.release(session)`: runs `trk claim release --as herdr --session S --json`, raises `TrkError` on non-zero exit. (Write replies are not part of the read contract; no schema check.)
  - `reconcile`: whole pass under `locked(state_path)`. After the routing loop: build `items` with `show` for each claim item, its root and each bound root (cached); `Snapshot(claims, items, open_asks, panes, herdr.workspaces(), now)`; `plan = decide(...)`. Dry run returns routing actions + `plan.effects` before any act. Act each effect, recording as it goes, then set `state.bindings`, `state.seen`, drop `plan.forget` keys from `state.reported`, save.
  - `cli._pass` passes `cfg=load_config(config_path())`.
- [ ] **Step 4: run** `pytest -q` — all pass.
- [ ] **Step 5: commit** `reconcile: act on the Space plan; trk claim release; sessions.jsonl`

### Task 6: `hx start <root>`

**Files:** Create `src/hx/start.py`. Modify `src/hx/cli.py`. Test `tests/test_start.py`.

**Produces:** `start(trk, herdr, state_path, cfg, root, now=None) -> str`.

Rules: the item must be a root (`parent` null) and open; a live binding means "already open" (no second Space). Role `orchestrator` when the root has an open non-ask child, else `solo`. Label `<root> <title cut to 40>`; cwd = first repo that is a directory, else `$HOME`; env `HX_ROLE`, `HX_ROOT`; tab 1 renamed to the role; the role's command template is run in the root pane. The binding is written under the state lock.

- [ ] **Step 1: failing tests**

```python
import json

import pytest

from hx.config import load_config
from hx.start import start, StartError
from hx.tracker import Trk
from tests.conftest import FakeHerdr


def go(trk_home, tmp_path, h, root):
    return start(Trk(home=str(trk_home)), h, tmp_path / "state.json", load_config(tmp_path / "none"), root)


def test_childless_root_starts_solo(trk_home, sh, tmp_path):
    r = sh("new", "project", "Solo thing")["item"]["id"]
    h = FakeHerdr([])
    go(trk_home, tmp_path, h, r)
    names = [c[0] for c in h.calls]
    assert names == ["create_space", "rename_tab", "run"]
    assert h.calls[0][1] == f"{r} Solo thing" and h.calls[0][3] == {"HX_ROLE": "solo", "HX_ROOT": r}
    assert h.calls[2] == ("run", "wNEW:p1", f"claude --model claude-sonnet-5-5 '/hx-work {r}'")
    b = json.loads((tmp_path / "state.json").read_text())["bindings"][r]
    assert b["origin"] == "hx" and b["role"] == "solo" and b["pane"] == "wNEW:p1"


def test_root_with_children_starts_an_orchestrator(trk_home, tree, tmp_path):
    h = FakeHerdr([])
    go(trk_home, tmp_path, h, tree["program"])
    assert "/hx-orchestrate" in h.calls[2][2] and h.calls[1] == ("rename_tab", "wNEW:t1", "orchestrator")


def test_refuses_a_non_root(trk_home, tree, tmp_path):
    with pytest.raises(StartError, match="not a root"):
        go(trk_home, tmp_path, FakeHerdr([]), tree["ticket"])


def test_second_start_does_not_open_a_duplicate(trk_home, sh, tmp_path):
    from hx.herdr import Workspace
    r = sh("new", "project", "Once")["item"]["id"]
    go(trk_home, tmp_path, FakeHerdr([]), r)
    h = FakeHerdr([], workspaces=[Workspace("wNEW", "")])
    assert "already open" in go(trk_home, tmp_path, h, r) and h.calls == []
```

- [ ] **Step 2: run** — FAIL. **Step 3: implement** `start.py` and the `hx start <root>` subcommand (prints the result line; `StartError` → stderr, exit 1).
- [ ] **Step 4: run** `pytest -q`. **Step 5: commit** `hx start: open a Space for a root`

### Task 7: live dry run and record

**Files:** Create `docs/checks/2026-10-12-slice-2-live-dry-run.md`.

- [ ] **Step 1:** `hx reconcile --dry-run` against the live store and herdr (read only). Expected: exit 0; every `Release` listed names a session that is in no live pane; no `Close`; `Report`s only for workspaces whose panes hold claims.
- [ ] **Step 2:** record trk version, head, claims, the decided effects, and any surprise. Send it to the tracker session.
- [ ] **Step 3: commit** `docs: slice 2 live dry run`
