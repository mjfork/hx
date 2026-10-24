# herdr-extension Slice 1: Ask Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A zero-token watcher (`hx`) that reads the `trk` tracker and delivers every open ask to the right place:
- to the right agent, by typing one line into its herdr pane while the agent is idle;
- or to Michael, through the console inbox and the needs-you queue.

When an ask is answered or withdrawn, it wakes the agent that asked. When an ask is withdrawn because its parent closed, it clears the ask quietly.

**Architecture:**
- The tracker is read only through `trk … --json`, with every reply validated against the tracker's own contract files.
- Two pure functions decide what to do: feed cursor rules, and routing.
- Thin adapters act on the decisions: the herdr CLI, an inbox log file, and the needs-you queue directory.
- A small state file makes every action happen once, even across restarts and store restores.
- `hx reconcile` runs one pass. `hx watch` loops it under systemd.

**Tech Stack:** Python 3.12, `uv`, `pytest`, `jsonschema>=4.26` (with `referencing`). No other runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-10-11-herdr-extension-design.md` (approved 2026-10-11). This plan builds **Build order step 1** only. Spaces, orchestrator and worker launch, the restart cap and the cutover each get their own plan.

## Global Constraints

- The feed `schema` must be `1`. If any reply has another `schema`, the pass stops acting and raises one alert (spec, Error handling).
- The cursor is the pair (epoch, seq). Rebuild when the epoch changes or `head` drops below the stored seq. On the first poll, store the epoch without a rebuild (ADR 0002 point 2).
- Rows with `data.via == "migration"` never cause a wake or a notice.
- The plugin writes to `trk` only as role `herdr`, and only `claim release` and `sweep`. Slice 1 writes nothing to `trk`.
- Human channels are `console` and `queue` only, both sent at once, with no timed steps (Michael, 2026-10-11).
- Waking an agent sends **one** message per pane per pass, only while `agent_status` is `idle` or `done`.
- Match a pane to a session by `agent_session.value`, never by pane id.
- Tests never touch the live store (`~/.local/share/trk`) or Michael's herdr. Integration tests use a throwaway `TRK_HOME` and a fake herdr adapter.
- Contract files are read from `~/projects/beads-alternative/contract/`, overridable by `HX_TRK_CONTRACT`. Never copy them into this repo.
- State lives in `~/.local/state/herdr-extension/` (overridable by `HX_STATE_DIR`). Config lives at `~/.config/herdr-extension/config.toml` (overridable by `HX_CONFIG`).
- Commit messages carry no `Co-Authored-By` line and no "Generated with" line (Michael's global rule).
- **Deviation from the spec, on purpose:** the spec says inotify on `feed.head`. Python's stdlib has no inotify, so `hx watch` polls the file's mtime once a second. That keeps the spec's "within about a second" with no new dependency.

## Review Focus

1. **An ask to Michael whose asking agent has no live pane.** The queue drops items whose `pane` is not a live agent pane. Expected: the console line still appears. The queue item is written when a live pane exists: the asker's pane, else the orchestrator's. Until then the queue step is retried silently each pass, never marked done. *(Test: Task 6, `test_queue_waits_for_a_live_pane`.)*
2. **The asking agent is working when the answer lands.** Expected: no text is typed into a busy pane. The wake waits until the pane is idle, then goes out once. *(Test: Task 5, `test_wake_held_while_working`.)*
3. **The watcher restarts after delivering half a batch.** Expected: nothing is delivered twice. Every action key is recorded before the cursor moves. *(Test: Task 7, `test_second_pass_sends_nothing`.)*
4. **The store is restored from a backup** (epoch changes, seqs reused). Expected: open asks that were already delivered are not re-sent, because delivery keys are per ask and per opening. Asks opened after the restore are delivered. *(Test: Task 3, `test_new_epoch_rebuilds_but_keeps_done_keys`.)*
5. **`trk` fails or returns invalid JSON mid-pass.** Expected: the pass acts on nothing, the cursor does not move, and the next pass retries. *(Test: Task 7, `test_trk_failure_moves_nothing`.)*

---

## File Structure

```
herdr-extension/
  pyproject.toml                   # package hx, entry point `hx`
  src/hx/__init__.py
  src/hx/tracker.py                # run trk, validate against contract/, TrkError
  src/hx/state.py                  # State dataclass, load/save atomically
  src/hx/feed.py                   # cursor + epoch rules, quiet rows, ask bookkeeping (pure)
  src/hx/herdr.py                  # Pane, list_panes(), wake(): thin herdr CLI adapter
  src/hx/routing.py                # route(...) -> list[Action] (pure)
  src/hx/channels.py               # Console (inbox.log), Queue (~/.herdr-queue), act()
  src/hx/reconcile.py              # one pass: read, decide, act, persist
  src/hx/config.py                 # load config.toml (humans → channels)
  src/hx/cli.py                    # hx reconcile [--dry-run], hx watch
  systemd/herdr-extension.service  # user unit running `hx watch`
  tests/conftest.py                # throwaway TRK_HOME fixture, FakeHerdr
  tests/test_tracker.py  tests/test_state.py  tests/test_feed.py
  tests/test_routing.py  tests/test_channels.py  tests/test_reconcile.py
```

---

### Task 1: Project scaffold and the contract-checked tracker reader

**Files:**
- Create: `pyproject.toml`, `src/hx/__init__.py`, `src/hx/tracker.py`
- Test: `tests/conftest.py`, `tests/test_tracker.py`

**Interfaces:**
- Produces:
  - `class TrkError(Exception)`
  - `class Trk`:
    - `__init__(self, home: str | None = None, bin: str = "trk", contract_dir: str | None = None)`
    - `call(self, contract: str, *args: str) -> dict`: runs `trk <args> --json` and validates against `contract/<contract>.schema.json`.
    - Convenience methods: `changes(since: int) -> dict`, `waiting() -> dict`, `claims() -> dict`, `show(item_id: str) -> dict`.
  - Fixture `trk_home` (path), plus a helper `sh(*args, stdin=None) -> dict` for driving a throwaway store in tests.

- [ ] **Step 1: Write `pyproject.toml` and the package marker**

```toml
[project]
name = "herdr-extension"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["jsonschema>=4.26"]

[project.scripts]
hx = "hx.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/hx"]

[dependency-groups]
dev = ["pytest>=8"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```

`tests/__init__.py` is empty. It lets tests import `tests.conftest`.

`src/hx/__init__.py`:

```python
"""herdr-extension: the zero-token watcher between trk and herdr."""
```

Run: `cd ~/projects/herdr-extension && uv sync`
Expected: it creates `.venv` and installs jsonschema and pytest.

- [ ] **Step 2: Write the test fixtures**

`tests/conftest.py`:

```python
import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def trk_home(tmp_path, monkeypatch):
    """A throwaway trk store: never the live one."""
    home = tmp_path / "trk"
    home.mkdir()
    monkeypatch.setenv("TRK_HOME", str(home))
    run(home, "init", "--me", "michael")
    run(home, "actor", "add", "worker", "--kind", "agent")
    run(home, "actor", "add", "orchestrator", "--kind", "agent")
    return home


def run(home: Path, *args: str, stdin: str | None = None) -> dict:
    env = {**os.environ, "TRK_HOME": str(home)}
    out = subprocess.run(["trk", *args, "--json"], input=stdin, capture_output=True,
                         text=True, env=env)
    data = json.loads(out.stdout or "{}")
    if out.returncode != 0:
        raise AssertionError(f"trk {' '.join(args)} failed: {data}")
    return data


@pytest.fixture
def sh(trk_home):
    """sh("claim", id, "--as", "worker", "--session", "s1") drives the throwaway store."""
    return lambda *a, stdin=None: run(trk_home, *a, stdin=stdin)


@pytest.fixture
def tree(sh, tmp_path):
    """A program with one self-contained ticket, claimed by worker session s-worker."""
    (tmp_path / "cb.md").write_text("now\n")
    (tmp_path / "db.md").write_text("want\n")
    prog = sh("new", "program", "Prog", "--criterion", "c1")["item"]["id"]
    tick = sh("new", "work-item", "Tick", "--form", "self-contained", "--parent", prog,
              "--section", f"current-behaviour=@{tmp_path / 'cb.md'}",
              "--section", f"desired-behaviour=@{tmp_path / 'db.md'}",
              "--criterion", "c1")["item"]["id"]
    sh("claim", tick, "--as", "worker", "--session", "s-worker")
    return {"program": prog, "ticket": tick}
```

- [ ] **Step 3: Write the failing tests**

`tests/test_tracker.py`:

```python
import pytest

from hx.tracker import Trk, TrkError


def test_changes_validates_against_contract(trk_home):
    reply = Trk(home=str(trk_home)).changes(0)
    assert reply["schema"] == 1
    assert reply["epoch"]
    assert [r["change"] for r in reply["changes"]][:1] == ["actor.added"]


def test_waiting_lists_an_open_ask(trk_home, sh, tree):
    sh("ask", tree["ticket"], "Colour?", "--to", "product", "--question", "-",
       "--as", "worker", "--session", "s-worker", stdin="Which colour?")
    asks = Trk(home=str(trk_home)).waiting()["asks"]
    assert [(a["addressee"], a["asker"]) for a in asks] == [("michael", "worker")]


def test_failed_command_raises(trk_home):
    with pytest.raises(TrkError, match="show"):
        Trk(home=str(trk_home)).show("tr-nope0")


def test_reply_that_breaks_the_contract_raises(trk_home, tmp_path):
    fake = tmp_path / "trk"
    fake.write_text('#!/bin/sh\necho \'{"schema": 2, "claims": []}\'\n')
    fake.chmod(0o755)
    with pytest.raises(TrkError, match="contract"):
        Trk(home=str(trk_home), bin=str(fake)).claims()
```

- [ ] **Step 4: Run the tests and see them fail**

Run: `uv run pytest tests/test_tracker.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hx.tracker'`

- [ ] **Step 5: Write `src/hx/tracker.py`**

```python
"""The only door to trk: run `trk … --json`, check the reply against the tracker's contract."""
import functools
import json
import os
import subprocess
from pathlib import Path

import jsonschema
from referencing import Registry, Resource

DEFAULT_CONTRACT = Path.home() / "projects/beads-alternative/contract"


class TrkError(Exception):
    pass


@functools.cache
def _validators(contract_dir: str) -> dict[str, jsonschema.Draft202012Validator]:
    schemas = {p.name.removesuffix(".schema.json"): json.loads(p.read_text())
               for p in Path(contract_dir).glob("*.schema.json")}
    registry = Registry().with_resources(
        [(s["$id"], Resource.from_contents(s)) for s in schemas.values()])
    return {name: jsonschema.Draft202012Validator(s, registry=registry)
            for name, s in schemas.items()}


class Trk:
    def __init__(self, home: str | None = None, bin: str = "trk", contract_dir: str | None = None):
        self.home = home
        self.bin = bin
        self.contract_dir = contract_dir or os.environ.get("HX_TRK_CONTRACT", str(DEFAULT_CONTRACT))

    def call(self, contract: str, *args: str) -> dict:
        env = dict(os.environ)
        if self.home:
            env["TRK_HOME"] = self.home
        try:
            out = subprocess.run([self.bin, *args, "--json"], capture_output=True, text=True,
                                 env=env, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise TrkError(f"trk {args[0]}: {e}") from e
        try:
            data = json.loads(out.stdout)
        except json.JSONDecodeError as e:
            raise TrkError(f"trk {args[0]}: not JSON (exit {out.returncode})") from e
        if out.returncode != 0:
            raise TrkError(f"trk {args[0]}: {data.get('error', data)}")
        errors = list(_validators(self.contract_dir)[contract].iter_errors(data))
        if errors:
            raise TrkError(f"trk {args[0]}: reply breaks contract {contract}: {errors[0].message}")
        return data

    def changes(self, since: int) -> dict:
        return self.call("changes", "changes", "--since", str(since))

    def waiting(self) -> dict:
        return self.call("waiting", "waiting", "--all")

    def claims(self) -> dict:
        return self.call("claims", "claims")

    def show(self, item_id: str) -> dict:
        return self.call("show", "show", item_id)
```

- [ ] **Step 6: Run the tests and see them pass**

Run: `uv run pytest tests/test_tracker.py -v`
Expected: 4 passed.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/hx/__init__.py src/hx/tracker.py tests/__init__.py tests/conftest.py tests/test_tracker.py
git commit -m "tracker: contract-checked trk reader"
```

---

### Task 2: The state file

**Files:**
- Create: `src/hx/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Produces:
  - `@dataclass class State`, with these fields and defaults:
    - `epoch: str | None = None`
    - `seq: int = 0`
    - `asks: dict[str, AskInfo]`, keyed by ask id
    - `done: dict[str, str]`, mapping action key to ISO time
    - `bindings: dict[str, dict]`, which stays empty in slice 1
  - `@dataclass class AskInfo`:
    - `session: str | None`: the asker's session, from the opening row;
    - `gen: int`: the seq of the latest `ask.opened`, `ask.reopened` or `ask.readdressed` row;
    - `title: str`;
    - `question: str`.
  - `load(path: Path) -> State`
  - `save(state: State, path: Path) -> None`, atomic: write a temp file, then `os.replace`.
  - `state_dir() -> Path`: `$HX_STATE_DIR` or `~/.local/state/herdr-extension`, created if missing.

- [ ] **Step 1: Write the failing tests**

`tests/test_state.py`:

```python
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
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest tests/test_state.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hx.state'`

- [ ] **Step 3: Write `src/hx/state.py`**

```python
"""The watcher's own memory: cursor, ask bookkeeping, and which action keys are done."""
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class AskInfo:
    session: str | None   # the asker's Claude session, from the opening row
    gen: int              # seq of the latest ask.opened / ask.reopened / ask.readdressed
    title: str
    question: str


@dataclass
class State:
    epoch: str | None = None
    seq: int = 0
    asks: dict[str, AskInfo] = field(default_factory=dict)
    done: dict[str, str] = field(default_factory=dict)
    bindings: dict[str, dict] = field(default_factory=dict)


def state_dir() -> Path:
    d = Path(os.environ.get("HX_STATE_DIR", Path.home() / ".local/state/herdr-extension"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def load(path: Path) -> State:
    if not path.exists():
        return State()
    raw = json.loads(path.read_text())
    raw["asks"] = {k: AskInfo(**v) for k, v in raw.get("asks", {}).items()}
    return State(**raw)


def save(state: State, path: Path) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=1, sort_keys=True))
    os.replace(tmp, path)
```

- [ ] **Step 4: Run them and see them pass**

Run: `uv run pytest tests/test_state.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/hx/state.py tests/test_state.py
git commit -m "state: atomic state file with ask bookkeeping and done keys"
```

---

### Task 3: Feed cursor, epoch rules and ask bookkeeping

**Files:**
- Create: `src/hx/feed.py`
- Test: `tests/test_feed.py`

**Interfaces:**
- Consumes: `State`, `AskInfo` (Task 2), and the `changes` reply shape (Task 1).
- Produces:
  - `class SchemaChanged(Exception)`
  - `@dataclass class FeedResult`:
    - `rebuilt: bool`
    - `closed: list[dict]`: the non-quiet `ask.answered` and `ask.withdrawn` rows seen in this reply.
  - `advance(state: State, reply: dict) -> FeedResult`. It is pure and changes only `state`:
    - It raises `SchemaChanged` if `reply["schema"] != 1`.
    - On the first poll it stores the epoch.
    - On a new epoch, or when `head < state.seq`, it clears `state.asks`, sets `seq = 0`, keeps `done`, and returns `rebuilt=True`. The caller then re-reads from 0.
    - Otherwise it records an `AskInfo` for every `ask.opened` and `ask.reopened` row, and bumps `gen` on every `ask.readdressed` row.
    - It drops `state.asks[id]` on `ask.answered` and `ask.withdrawn`, after copying the row into `closed`.
    - It sets `state.seq = reply["next"]`.
    - A reply read from seq 0 (the first poll, or after a rebuild) is history. It records asks but puts nothing in `closed`, so old answers never wake anyone.
  - `is_quiet(row: dict) -> bool`

- [ ] **Step 1: Write the failing tests**

`tests/test_feed.py`:

```python
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
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest tests/test_feed.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hx.feed'`

- [ ] **Step 3: Write `src/hx/feed.py`**

```python
"""Cursor rules (ADR 0002 point 2) and what the feed tells us about asks. Pure: no I/O."""
from dataclasses import dataclass, field

from hx.state import AskInfo, State


class SchemaChanged(Exception):
    pass


@dataclass
class FeedResult:
    rebuilt: bool = False
    closed: list[dict] = field(default_factory=list)


def is_quiet(row: dict) -> bool:
    return (row.get("data") or {}).get("via") == "migration"


def advance(state: State, reply: dict) -> FeedResult:
    if reply["schema"] != 1:
        raise SchemaChanged(f"feed schema {reply['schema']}")
    if state.epoch is None:
        state.epoch = reply["epoch"]
    elif reply["epoch"] != state.epoch or reply["head"] < state.seq:
        state.epoch, state.seq, state.asks = reply["epoch"], 0, {}
        return FeedResult(rebuilt=True)
    result = FeedResult()
    replay = state.seq == 0          # history: record asks, but wake and clear nobody
    for row in reply["changes"]:
        kind, ask, data = row["change"], row["item"], row.get("data") or {}
        if kind in ("ask.opened", "ask.reopened"):
            state.asks[ask] = AskInfo(row.get("session"), row["seq"], row.get("title") or "",
                                      data.get("question", ""))
        elif kind == "ask.readdressed" and ask in state.asks:
            state.asks[ask].gen = row["seq"]
        elif kind in ("ask.answered", "ask.withdrawn"):
            if not is_quiet(row) and not replay:
                result.closed.append({**row, "asker_session": getattr(state.asks.get(ask), "session", None)})
            state.asks.pop(ask, None)
    state.seq = reply["next"]
    return result
```

- [ ] **Step 4: Run them and see them pass**

Run: `uv run pytest tests/test_feed.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/hx/feed.py tests/test_feed.py
git commit -m "feed: epoch/seq cursor, quiet migration rows, ask bookkeeping"
```

---

### Task 4: The herdr adapter

**Files:**
- Create: `src/hx/herdr.py`
- Test: `tests/test_herdr.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Pane`, with fields `pane_id: str`, `workspace_id: str`, `session: str | None`, `status: str` and `cwd: str`, plus a property `wakeable -> bool`, which is true when `status` is `idle` or `done`.
  - `class Herdr`:
    - `__init__(self, bin: str = "herdr")`
    - `panes(self) -> list[Pane]`: from `herdr pane list`; raises `HerdrError` on failure.
    - `wake(self, pane_id: str, text: str) -> None`: runs `herdr pane run <pane> <text>`, which sends the text and Enter.
  - `class HerdrError(Exception)`
  - `parse_panes(reply: dict) -> list[Pane]`: pure, so it can be tested.
  - Tests use a `FakeHerdr` with the same two methods. It is defined in `tests/conftest.py` in Task 7.

- [ ] **Step 1: Write the failing test**

`tests/test_herdr.py`:

```python
from hx.herdr import Pane, parse_panes

REPLY = {"result": {"type": "pane_list", "panes": [
    {"pane_id": "wB9:p3", "workspace_id": "wB9", "agent_status": "done", "cwd": "/x",
     "agent_session": {"agent": "claude", "kind": "id", "value": "s-worker"}},
    {"pane_id": "wB9:p4", "workspace_id": "wB9", "agent_status": "working", "cwd": "/y"},
]}}


def test_parse_panes_reads_session_and_status():
    assert parse_panes(REPLY) == [
        Pane("wB9:p3", "wB9", "s-worker", "done", "/x"),
        Pane("wB9:p4", "wB9", None, "working", "/y"),
    ]


def test_only_idle_or_done_is_wakeable():
    assert Pane("p", "w", "s", "idle", "/").wakeable
    assert Pane("p", "w", "s", "done", "/").wakeable
    assert not Pane("p", "w", "s", "working", "/").wakeable
    assert not Pane("p", "w", "s", "blocked", "/").wakeable
```

- [ ] **Step 2: Run it and see it fail**

Run: `uv run pytest tests/test_herdr.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hx.herdr'`

- [ ] **Step 3: Write `src/hx/herdr.py`**

```python
"""Thin adapter over the herdr CLI. Panes are matched by Claude session, never by pane id."""
import json
import subprocess
from dataclasses import dataclass


class HerdrError(Exception):
    pass


@dataclass(frozen=True)
class Pane:
    pane_id: str
    workspace_id: str
    session: str | None
    status: str
    cwd: str

    @property
    def wakeable(self) -> bool:
        return self.status in ("idle", "done")


def parse_panes(reply: dict) -> list[Pane]:
    return [Pane(p["pane_id"], p["workspace_id"], (p.get("agent_session") or {}).get("value"),
                 p.get("agent_status", "unknown"), p.get("cwd", ""))
            for p in reply["result"]["panes"]]


class Herdr:
    def __init__(self, bin: str = "herdr"):
        self.bin = bin

    def _run(self, *args: str) -> str:
        try:
            out = subprocess.run([self.bin, *args], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise HerdrError(str(e)) from e
        if out.returncode != 0:
            raise HerdrError(f"herdr {' '.join(args[:2])}: {out.stderr.strip()[:200]}")
        return out.stdout

    def panes(self) -> list[Pane]:
        return parse_panes(json.loads(self._run("pane", "list")))

    def wake(self, pane_id: str, text: str) -> None:
        self._run("pane", "run", pane_id, text)
```

- [ ] **Step 4: Run it and see it pass**

Run: `uv run pytest tests/test_herdr.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/hx/herdr.py tests/test_herdr.py
git commit -m "herdr: pane list by session, wake by pane run"
```

---

### Task 5: Routing, as a pure function

**Files:**
- Create: `src/hx/routing.py`
- Test: `tests/test_routing.py`

**Interfaces:**
- Consumes: `State`/`AskInfo` (Task 2), `Pane` (Task 4), `trk waiting` items (Task 1), `FeedResult.closed` rows (Task 3).
- Produces (all frozen dataclasses with a `key: str`):
  - `Notify(key, person: str, channel: str, ask: str, text: str, pane: str | None)`. For `channel == "queue"`, `pane` is the live pane the queue item hangs on. It is `None` when no live pane exists yet, and the queue step then retries.
  - `Wake(key, pane: str, text: str)`: one per pane per pass, batching every ask for it.
  - `Clear(key, ask: str)`: removes the ask's queue item and writes a console line.
- `route(open_asks: list[dict], closed: list[dict], claims: list[dict], ancestors: dict[str, list[str]], state: State, panes: list[Pane], humans: dict[str, list[str]]) -> list[Notify | Wake | Clear]`
  - It returns only actions whose `key` is not in `state.done`.
  - `ancestors[ask_id]` lists the ask's ancestor ids, nearest first, starting with its parent.

**Routing rules (spec Section 1):**
- **An open ask addressed to an agent.** Take the first match in this order and wake its pane:
  1. a live claim with `actor == addressee` on the nearest ancestor that has one;
  2. the orchestrator session of `state.bindings[root]` (empty in slice 1);
  3. the effective owner's human channels.

  A session with no live pane, or a pane that is not wakeable, falls through to the next step only when it has no pane at all. A pane that is busy holds the wake: no action now, retried next pass.
- **An open ask addressed to a human.** One `Notify` per channel in `humans[addressee]`. When that person has no entry, use `["console"]`.
  - Keys: `deliver:<ask>:<gen>:<channel>`.
  - The queue pane is the asker's live pane, else the orchestrator's live pane, else `None`.
- **A closed row with `data.reason == "parent_closed"`:** only `Clear`.
- **Any other closed row:** `Clear`, plus one wake to the first live, wakeable pane of these:
  1. the asker's session;
  2. `parent_claim_session`, when `parent_claim_live`;
  3. the orchestrator.

  The key is `answered:<ask>:<seq>`. The text is `[hx] tr-a answered: <outcome>. Run trk show tr-a.` for an answer, and `[hx] tr-a withdrawn: <reason or resolution>.` for a withdrawal.

- [ ] **Step 1: Write the failing tests**

`tests/test_routing.py`:

```python
from hx.herdr import Pane
from hx.routing import Clear, Notify, Wake, route
from hx.state import AskInfo, State

IDLE = Pane("w1:p2", "w1", "s-worker", "done", "/")
BUSY = Pane("w1:p2", "w1", "s-worker", "working", "/")


def ask(id="tr-a", addressee="michael", kind="human", root="tr-p", owner="michael"):
    return {"id": id, "title": "Colour?", "addressee": addressee, "addressee_kind": kind,
            "root": root, "effective_owner": owner}


def st(**asks):
    return State(epoch="e", seq=9, asks={k: AskInfo("s-worker", 5, "Colour?", "Which?")
                                         for k in asks or {"tr-a": 1}})


def test_human_ask_goes_to_every_channel_at_once():
    acts = route([ask()], [], [], {"tr-a": ["tr-t", "tr-p"]}, st(),
                 [IDLE], {"michael": ["console", "queue"]})
    assert [(a.channel, a.pane) for a in acts] == [("console", None), ("queue", "w1:p2")]
    assert acts[0].key == "deliver:tr-a:5:console"


def test_done_keys_are_not_repeated():
    s = st(); s.done["deliver:tr-a:5:console"] = "t"
    acts = route([ask()], [], [], {"tr-a": []}, s, [IDLE], {"michael": ["console"]})
    assert acts == []


def test_queue_waits_for_a_live_pane():
    acts = route([ask()], [], [], {"tr-a": []}, st(), [], {"michael": ["queue"]})
    assert [(a.channel, a.pane) for a in acts] == [("queue", None)]


def test_agent_ask_wakes_the_claimant_on_the_nearest_ancestor():
    claims = [{"item": "tr-t", "actor": "worker", "session": "s-worker"}]
    acts = route([ask(addressee="worker", kind="agent")], [], claims,
                 {"tr-a": ["tr-t", "tr-p"]}, st(), [IDLE], {})
    assert len(acts) == 1 and isinstance(acts[0], Wake) and acts[0].pane == "w1:p2"
    assert "tr-a" in acts[0].text


def test_two_asks_for_one_pane_are_one_message():
    claims = [{"item": "tr-t", "actor": "worker", "session": "s-worker"}]
    acts = route([ask("tr-a", "worker", "agent"), ask("tr-b", "worker", "agent")], [], claims,
                 {"tr-a": ["tr-t"], "tr-b": ["tr-t"]}, st(**{"tr-a": 1, "tr-b": 1}), [IDLE], {})
    assert len(acts) == 1 and "2 asks" in acts[0].text


def test_wake_held_while_working():
    claims = [{"item": "tr-t", "actor": "worker", "session": "s-worker"}]
    assert route([ask(addressee="worker", kind="agent")], [], claims,
                 {"tr-a": ["tr-t"]}, st(), [BUSY], {}) == []


def test_agent_ask_with_no_agent_falls_to_owner_channels():
    acts = route([ask(addressee="worker", kind="agent")], [], [], {"tr-a": ["tr-t"]},
                 st(), [], {"michael": ["console"]})
    assert [(type(a), a.person) for a in acts] == [(Notify, "michael")]


def test_answer_wakes_the_asker_and_clears():
    row = {"seq": 12, "change": "ask.answered", "item": "tr-a", "root": "tr-p",
           "asker_session": "s-worker", "data": {"outcome": "Blue", "parent_claim_session": None,
                                                   "parent_claim_live": False}}
    acts = route([], [row], [], {}, st(), [IDLE], {})
    assert [type(a) for a in acts] == [Clear, Wake]
    assert acts[1].text.startswith("[hx] tr-a answered: Blue")


def test_parent_closed_clears_quietly():
    row = {"seq": 12, "change": "ask.withdrawn", "item": "tr-a", "root": "tr-p",
           "asker_session": "s-worker", "data": {"reason": "parent_closed",
                                                   "parent_claim_session": None,
                                                   "parent_claim_live": False}}
    assert [type(a) for a in route([], [row], [], {}, st(), [IDLE], {})] == [Clear]
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest tests/test_routing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hx.routing'`

- [ ] **Step 3: Write `src/hx/routing.py`**

```python
"""Who hears about an ask (spec Section 1, ADR 0002 point 10). Pure: snapshots in, actions out."""
from dataclasses import dataclass

from hx.herdr import Pane
from hx.state import State


@dataclass(frozen=True)
class Notify:
    key: str
    person: str
    channel: str
    ask: str
    text: str
    pane: str | None


@dataclass(frozen=True)
class Wake:
    key: str
    pane: str
    text: str


@dataclass(frozen=True)
class Clear:
    key: str
    ask: str


def _pane_of(session: str | None, panes: list[Pane]) -> Pane | None:
    return next((p for p in panes if session and p.session == session), None)


def _orchestrator(root: str, state: State) -> str | None:
    return (state.bindings.get(root) or {}).get("orchestrator")


def _human(a: dict, person: str, state: State, panes, humans) -> list[Notify]:
    info = state.asks.get(a["id"])
    gen = info.gen if info else 0
    text = f"{a['id']} {a['title']}: {info.question if info else ''}".strip()
    asker = _pane_of(info.session if info else None, panes) or \
        _pane_of(_orchestrator(a["root"], state), panes)
    return [Notify(f"deliver:{a['id']}:{gen}:{ch}", person, ch, a["id"], text,
                   asker.pane_id if (ch == "queue" and asker) else None)
            for ch in humans.get(person, ["console"])]


def route(open_asks, closed, claims, ancestors, state: State, panes, humans) -> list:
    acts: list = []
    wakes: dict[str, list[tuple[str, str]]] = {}   # pane -> [(key, line)]
    for a in open_asks:
        if a["addressee_kind"] == "human":
            acts += _human(a, a["addressee"], state, panes, humans)
            continue
        gen = state.asks[a["id"]].gen if a["id"] in state.asks else 0
        sessions = [next((c["session"] for c in claims
                          if c["item"] == anc and c["actor"] == a["addressee"]), None)
                    for anc in ancestors.get(a["id"], [])]
        candidates = [s for s in sessions if s] + [_orchestrator(a["root"], state)]
        pane = next((p for s in candidates if (p := _pane_of(s, panes))), None)
        if pane is None:
            acts += _human(a, a["effective_owner"], state, panes, humans)
        elif pane.wakeable:
            wakes.setdefault(pane.pane_id, []).append(
                (f"wake:{a['id']}:{gen}:{pane.session}", f"{a['id']} \"{a['title']}\""))
    for row in closed:
        ask, data = row["item"], row["data"]
        acts.append(Clear(f"clear:{ask}:{row['seq']}", ask))
        if data.get("reason") == "parent_closed":
            continue
        live_parent = data.get("parent_claim_session") if data.get("parent_claim_live") else None
        for s in (row.get("asker_session"), live_parent, _orchestrator(row["root"], state)):
            p = _pane_of(s, panes)
            if p and p.wakeable:
                verb = "answered" if row["change"] == "ask.answered" else "withdrawn"
                why = data.get("outcome") or data.get("reason") or data.get("resolution")
                tail = " Run trk show %s." % ask if verb == "answered" else ""
                acts.append(Wake(f"answered:{ask}:{row['seq']}", p.pane_id,
                                 f"[hx] {ask} {verb}: {why}.{tail}"))
                break
    for pane_id, items in wakes.items():
        todo = [(k, line) for k, line in items if k not in state.done]
        if todo:
            head = f"[hx] {len(todo)} asks for you: " if len(todo) > 1 else "[hx] ask for you: "
            acts.append(Wake("+".join(k for k, _ in todo), pane_id,
                             head + " · ".join(l for _, l in todo) + ". Run trk show <id>."))
    return [a for a in acts if a.key not in state.done]
```

A batched `Wake` key joins the per-ask keys with `+`. Task 7's executor splits a key on `+` and records each part, so a later pass never repeats any ask in the batch.

- [ ] **Step 4: Run them and see them pass**

Run: `uv run pytest tests/test_routing.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add src/hx/routing.py tests/test_routing.py
git commit -m "routing: pure ask routing with fallbacks, batching and quiet clears"
```

---

### Task 6: Channels: the console inbox and the needs-you queue

**Files:**
- Create: `src/hx/channels.py`
- Test: `tests/test_channels.py`

**Interfaces:**
- Consumes: `Notify`, `Clear` (Task 5).
- Produces: `class Channels`:
  - `__init__(self, inbox: Path, queue_dir: Path, workspaces: dict[str, str])`, where `workspaces` maps pane id to workspace id;
  - `notify(self, n: Notify) -> bool`: returns `False` when the queue has no live pane yet, so the caller must not mark the action done;
  - `clear(self, c: Clear) -> None`.
- Files written:
  - The inbox is one line per event: `2026-10-11T15:10:39Z  ASK  tr-a  michael  Colour?: Which colour?` (or `CLEARED` in place of `ASK`).
  - A queue item is `queue_dir/hx_<ask>.json`, in the needs-you shape: `{"pane", "workspace", "ask", "cwd", "ts"}`. The `hx_` prefix keeps it from colliding with an agent's own `<pane>.json` item.

- [ ] **Step 1: Write the failing tests**

`tests/test_channels.py`:

```python
import json

from hx.channels import Channels
from hx.routing import Clear, Notify


def ch(tmp_path):
    return Channels(tmp_path / "inbox.log", tmp_path / "q", {"w1:p2": "w1"})


def test_console_appends_a_line(tmp_path):
    assert ch(tmp_path).notify(Notify("k", "michael", "console", "tr-a", "tr-a Colour?: Which?", None))
    assert "ASK  tr-a  michael  tr-a Colour?: Which?" in (tmp_path / "inbox.log").read_text()


def test_queue_item_has_the_needs_you_shape(tmp_path):
    assert ch(tmp_path).notify(Notify("k", "michael", "queue", "tr-a", "tr-a Colour?: Which?", "w1:p2"))
    item = json.loads((tmp_path / "q" / "hx_tr-a.json").read_text())
    assert item["pane"] == "w1:p2" and item["workspace"] == "w1"
    assert item["ask"].startswith("**Ask:** tr-a Colour?: Which?")
    assert "trk answer tr-a" in item["ask"]


def test_queue_without_pane_is_not_done(tmp_path):
    assert ch(tmp_path).notify(Notify("k", "michael", "queue", "tr-a", "t", None)) is False
    assert not (tmp_path / "q").exists() or not list((tmp_path / "q").iterdir())


def test_clear_removes_queue_item_and_logs(tmp_path):
    c = ch(tmp_path)
    c.notify(Notify("k", "michael", "queue", "tr-a", "t", "w1:p2"))
    c.clear(Clear("c", "tr-a"))
    assert not (tmp_path / "q" / "hx_tr-a.json").exists()
    assert "CLEARED  tr-a" in (tmp_path / "inbox.log").read_text()
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest tests/test_channels.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hx.channels'`

- [ ] **Step 3: Write `src/hx/channels.py`**

```python
"""Human channels: console (inbox.log, shown by the inbox pane) and the needs-you queue."""
import json
import os
import time
from pathlib import Path

from hx.routing import Clear, Notify


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Channels:
    def __init__(self, inbox: Path, queue_dir: Path, workspaces: dict[str, str]):
        self.inbox, self.queue_dir, self.workspaces = inbox, queue_dir, workspaces

    def _log(self, line: str) -> None:
        self.inbox.parent.mkdir(parents=True, exist_ok=True)
        with self.inbox.open("a") as f:
            f.write(f"{_now()}  {line}\n")

    def notify(self, n: Notify) -> bool:
        if n.channel == "console":
            self._log(f"ASK  {n.ask}  {n.person}  {n.text}")
            return True
        if n.channel == "queue":
            if n.pane is None:
                return False
            self.queue_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            item = {"pane": n.pane, "workspace": self.workspaces.get(n.pane, ""),
                    "ask": f"**Ask:** {n.text}\n**Reply with:** `trk answer {n.ask} "
                           f"--outcome \"<one line>\" --answer -`",
                    "cwd": str(Path.home()), "ts": int(time.time())}
            tmp = self.queue_dir / f".hx_{n.ask}.tmp"
            tmp.write_text(json.dumps(item, indent=2))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.queue_dir / f"hx_{n.ask}.json")
            return True
        raise ValueError(f"unknown channel {n.channel}")

    def clear(self, c: Clear) -> None:
        (self.queue_dir / f"hx_{c.ask}.json").unlink(missing_ok=True)
        self._log(f"CLEARED  {c.ask}")
```

- [ ] **Step 4: Run them and see them pass**

Run: `uv run pytest tests/test_channels.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/hx/channels.py tests/test_channels.py
git commit -m "channels: console inbox and needs-you queue items"
```

---

### Task 7: One reconcile pass, end to end on a throwaway store

**Files:**
- Create: `src/hx/config.py`, `src/hx/reconcile.py`
- Modify: `tests/conftest.py` (add `FakeHerdr`)
- Test: `tests/test_reconcile.py`

**Interfaces:**
- Consumes: Tasks 1–6.
- Produces:
  - `load_humans(path: Path) -> dict[str, list[str]]` (in `config.py`). It reads `[humans.<id>] channels = [...]`. A missing file gives `{"michael": ["console", "queue"]}`.
  - `reconcile(trk: Trk, herdr, state_path: Path, inbox: Path, queue_dir: Path, humans: dict, dry_run: bool = False) -> list` (in `reconcile.py`). It returns the actions decided. Its steps:
    1. It loads state, then reads changes, re-reading from 0 once on a rebuild. It does this with a scratch copy of the state.
    2. It reads `waiting`, `claims` and the herdr panes.
    3. It builds `ancestors` by walking `show` up from each open ask's parent. The walk is cached for the pass.
    4. It calls `route`, then acts, unless `dry_run`.
    5. For each action that succeeded, it records every `+`-split part of its key in `state.done`.
    6. It saves the scratch state as the real state only after all actions ran. Any exception before the save leaves the state file untouched.
  - In `tests/conftest.py`, `FakeHerdr(panes: list[Pane])` records `wakes: list[tuple[str, str]]`.

- [ ] **Step 1: Add `FakeHerdr` to `tests/conftest.py`**

```python
from hx.herdr import Pane


class FakeHerdr:
    def __init__(self, panes: list[Pane]):
        self._panes = panes
        self.wakes: list[tuple[str, str]] = []

    def panes(self) -> list[Pane]:
        return self._panes

    def wake(self, pane_id: str, text: str) -> None:
        self.wakes.append((pane_id, text))
```

- [ ] **Step 2: Write the failing tests**

`tests/test_reconcile.py`:

```python
import json

import pytest

from hx.herdr import Pane
from hx.reconcile import reconcile
from hx.tracker import Trk, TrkError
from tests.conftest import FakeHerdr

WORKER = Pane("w1:p2", "w1", "s-worker", "done", "/")
HUMANS = {"michael": ["console", "queue"]}


def run(trk_home, tmp_path, herdr, dry=False):
    return reconcile(Trk(home=str(trk_home)), herdr, tmp_path / "state.json",
                     tmp_path / "inbox.log", tmp_path / "q", HUMANS, dry_run=dry)


def ask_michael(sh, tree):
    return sh("ask", tree["ticket"], "Colour?", "--to", "product", "--question", "-",
              "--as", "worker", "--session", "s-worker", stdin="Which colour?")["item"]["id"]


def test_ask_to_michael_reaches_console_and_queue(trk_home, sh, tree, tmp_path):
    a = ask_michael(sh, tree)
    run(trk_home, tmp_path, FakeHerdr([WORKER]))
    assert f"ASK  {a}  michael" in (tmp_path / "inbox.log").read_text()
    assert json.loads((tmp_path / "q" / f"hx_{a}.json").read_text())["pane"] == "w1:p2"


def test_second_pass_sends_nothing(trk_home, sh, tree, tmp_path):
    ask_michael(sh, tree)
    run(trk_home, tmp_path, FakeHerdr([WORKER]))
    lines = (tmp_path / "inbox.log").read_text()
    assert run(trk_home, tmp_path, FakeHerdr([WORKER])) == []
    assert (tmp_path / "inbox.log").read_text() == lines


def test_answer_wakes_the_asker_and_clears_queue(trk_home, sh, tree, tmp_path):
    a = ask_michael(sh, tree)
    run(trk_home, tmp_path, FakeHerdr([WORKER]))
    sh("answer", a, "--outcome", "Blue", "--answer", "-", stdin="Blue")
    h = FakeHerdr([WORKER])
    run(trk_home, tmp_path, h)
    assert h.wakes == [("w1:p2", f"[hx] {a} answered: Blue. Run trk show {a}.")]
    assert not (tmp_path / "q" / f"hx_{a}.json").exists()


def test_dry_run_acts_on_nothing(trk_home, sh, tree, tmp_path):
    ask_michael(sh, tree)
    acts = run(trk_home, tmp_path, FakeHerdr([WORKER]), dry=True)
    assert len(acts) == 2
    assert not (tmp_path / "inbox.log").exists() and not (tmp_path / "state.json").exists()


def test_trk_failure_moves_nothing(trk_home, sh, tree, tmp_path, monkeypatch):
    ask_michael(sh, tree)
    monkeypatch.setattr(Trk, "waiting", lambda self: (_ for _ in ()).throw(TrkError("boom")))
    with pytest.raises(TrkError):
        run(trk_home, tmp_path, FakeHerdr([WORKER]))
    assert not (tmp_path / "state.json").exists() and not (tmp_path / "inbox.log").exists()
```

- [ ] **Step 3: Run them and see them fail**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hx.reconcile'`

- [ ] **Step 4: Write `src/hx/config.py` and `src/hx/reconcile.py`**

`src/hx/config.py`:

```python
"""~/.config/herdr-extension/config.toml: which channels reach each person."""
import os
import tomllib
from pathlib import Path

DEFAULT = Path.home() / ".config/herdr-extension/config.toml"


def config_path() -> Path:
    return Path(os.environ.get("HX_CONFIG", DEFAULT))


def load_humans(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {"michael": ["console", "queue"]}
    raw = tomllib.loads(path.read_text())
    return {who: list(v.get("channels", ["console"])) for who, v in raw.get("humans", {}).items()}
```

`src/hx/reconcile.py`:

```python
"""One pass: read trk and herdr, decide, act, then persist. Safe to repeat at any time."""
import copy
import time
from pathlib import Path

from hx.channels import Channels
from hx.feed import advance
from hx.routing import Clear, Notify, Wake, route
from hx.state import load, save
from hx.tracker import Trk


def _ancestors(trk: Trk, open_asks: list[dict]) -> dict[str, list[str]]:
    cache: dict[str, str | None] = {}

    def parent_of(item: str) -> str | None:
        if item not in cache:
            cache[item] = trk.show(item)["item"]["parent"]
        return cache[item]

    out = {}
    for a in open_asks:
        chain, cur = [], a["parent"]["id"]
        while cur:
            chain.append(cur)
            cur = parent_of(cur)
        out[a["id"]] = chain
    return out


def reconcile(trk: Trk, herdr, state_path: Path, inbox: Path, queue_dir: Path,
              humans: dict, dry_run: bool = False) -> list:
    state = copy.deepcopy(load(state_path))
    result = advance(state, trk.changes(state.seq))
    if result.rebuilt:
        result = advance(state, trk.changes(0))
    open_asks = trk.waiting()["asks"]
    claims = trk.claims()["claims"]
    panes = herdr.panes()
    actions = route(open_asks, result.closed, claims, _ancestors(trk, open_asks),
                    state, panes, humans)
    if dry_run:
        return actions
    channels = Channels(inbox, queue_dir, {p.pane_id: p.workspace_id for p in panes})
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for a in actions:
        if isinstance(a, Notify):
            ok = channels.notify(a)
        elif isinstance(a, Clear):
            channels.clear(a)
            ok = True
        elif isinstance(a, Wake):
            herdr.wake(a.pane, a.text)
            ok = True
        if ok:
            for part in a.key.split("+"):
                state.done[part] = stamp
    save(state, state_path)
    return actions
```

- [ ] **Step 5: Run them and see them pass**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: 5 passed.

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest -v`
Expected: every test passes (35 tests).

- [ ] **Step 7: Commit**

```bash
git add src/hx/config.py src/hx/reconcile.py tests/conftest.py tests/test_reconcile.py
git commit -m "reconcile: one idempotent pass from trk to console, queue and wakes"
```

---

### Task 8: The `hx` CLI, `hx watch`, and the systemd unit

**Files:**
- Create: `src/hx/cli.py`, `systemd/herdr-extension.service`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `reconcile` (Task 7), `load_humans`/`config_path` (Task 7), `state_dir` (Task 2), `Trk`, `Herdr`.
- Produces:
  - `main(argv: list[str] | None = None) -> int`. Subcommands:
    - `hx reconcile [--dry-run]`: prints one line per action (`<Type> <key> <target>`) and returns 0.
    - `hx watch [--interval 1.0] [--tick 30]`: loops forever.
  - `feed_head_path() -> Path`: `$TRK_HOME/feed.head`, or `~/.local/share/trk/feed.head` when `TRK_HOME` is unset.
  - `watch_once(last: float | None, path: Path, run, now: float, last_run: float, tick: float) -> tuple[float | None, float]`: pure enough to test. It calls `run()` when `feed.head`'s mtime changed, or when `tick` seconds passed since `last_run`. It returns the new `(mtime, last_run)`.
  - When `reconcile` raises, `hx watch` logs one line to `watch.log` in the state dir and backs off: 1 s, doubling to at most 60 s. It never exits on a `TrkError` or a `HerdrError`.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py`:

```python
from hx.cli import main, watch_once
from tests.conftest import FakeHerdr


def test_watch_runs_on_head_change_and_on_tick(tmp_path):
    head = tmp_path / "feed.head"
    head.write_text("1")
    calls = []
    mt, lr = watch_once(None, head, lambda: calls.append(1), now=100.0, last_run=100.0, tick=30)
    assert calls == [1]
    mt, lr = watch_once(mt, head, lambda: calls.append(2), now=101.0, last_run=lr, tick=30)
    assert calls == [1]
    mt, lr = watch_once(mt, head, lambda: calls.append(3), now=200.0, last_run=lr, tick=30)
    assert calls == [1, 3]


def test_reconcile_dry_run_prints_actions(trk_home, sh, tree, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HX_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.setattr("hx.cli.Herdr", lambda: FakeHerdr([]))
    sh("ask", tree["ticket"], "Colour?", "--to", "product", "--question", "-",
       "--as", "worker", "--session", "s-worker", stdin="Which colour?")
    assert main(["reconcile", "--dry-run"]) == 0
    assert "Notify deliver:" in capsys.readouterr().out
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hx.cli'`

- [ ] **Step 3: Write `src/hx/cli.py`**

```python
"""hx reconcile [--dry-run] · hx watch: the commands systemd and people run."""
import argparse
import os
import sys
import time
from pathlib import Path

from hx.config import config_path, load_humans
from hx.herdr import Herdr, HerdrError
from hx.reconcile import reconcile
from hx.state import state_dir
from hx.tracker import Trk, TrkError

QUEUE_DIR = Path.home() / ".herdr-queue" / os.environ.get("HERDR_SESSION", "default")


def feed_head_path() -> Path:
    home = os.environ.get("TRK_HOME")
    return (Path(home) if home else Path.home() / ".local/share/trk") / "feed.head"


def _pass(dry_run: bool) -> list:
    d = state_dir()
    return reconcile(Trk(), Herdr(), d / "state.json", d / "inbox.log", QUEUE_DIR,
                     load_humans(config_path()), dry_run=dry_run)


def watch_once(last, path: Path, run, now: float, last_run: float, tick: float):
    mtime = path.stat().st_mtime if path.exists() else None
    if mtime != last or now - last_run >= tick:
        run()
        return mtime, now
    return mtime, last_run


def _watch(interval: float, tick: float) -> int:
    log = state_dir() / "watch.log"
    last, last_run, backoff = None, 0.0, 1.0
    while True:
        try:
            last, last_run = watch_once(last, feed_head_path(), lambda: _pass(False),
                                        time.time(), last_run, tick)
            backoff = 1.0
            time.sleep(interval)
        except (TrkError, HerdrError, OSError) as e:
            with log.open("a") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  ERROR  {e}\n")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hx")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reconcile")
    r.add_argument("--dry-run", action="store_true")
    w = sub.add_parser("watch")
    w.add_argument("--interval", type=float, default=1.0)
    w.add_argument("--tick", type=float, default=30.0)
    args = p.parse_args(argv)
    if args.cmd == "reconcile":
        for a in _pass(args.dry_run):
            target = getattr(a, "pane", None) or getattr(a, "person", None) or getattr(a, "ask", "")
            print(f"{type(a).__name__} {a.key} {target}")
        return 0
    return _watch(args.interval, args.tick)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Write `systemd/herdr-extension.service`**

```ini
[Unit]
Description=herdr-extension watcher (trk asks to herdr, console and needs-you queue)

[Service]
ExecStart=%h/.local/bin/hx watch
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

- [ ] **Step 5: Run the tests and see them pass**

Run: `uv run pytest -v`
Expected: every test passes (37 tests).

- [ ] **Step 6: Commit**

```bash
git add src/hx/cli.py systemd/herdr-extension.service tests/test_cli.py
git commit -m "cli: hx reconcile [--dry-run], hx watch with backoff, systemd unit"
```

---

### Task 9: Read-only check on the live store, and evidence for tr-yh17a

This task touches the live store, but only to read, plus the claim and evidence writes the tracker asked for. **Do not install or start the systemd service.** Starting the service is a separate decision for Michael.

**Files:**
- Create: `docs/checks/2026-10-11-live-dry-run.md`, the record of the run.

- [ ] **Step 1: Install `hx` on PATH, editable**

Run: `uv tool install -e ~/projects/herdr-extension`
Expected: `Installed 1 executable: hx`

- [ ] **Step 2: Run one dry pass against the live store and the real herdr**

Run: `HX_STATE_DIR=$(mktemp -d) hx reconcile --dry-run | tee /tmp/hx-live-dry-run.txt`

Expected: exit 0, with no `TrkError` or `HerdrError`. It prints one `Notify` or `Wake` line for each open ask on the live store, matching `trk waiting --all --json`.

A temporary state dir means the dry run marks nothing done in the real state.

- [ ] **Step 3: Exercise the four row kinds on a throwaway store**

The live store may have no fresh answered or withdrawn rows, so this step proves routing on all four kinds in a throwaway store.

Run: `uv run pytest tests/test_reconcile.py tests/test_routing.py -v 2>&1 | tee /tmp/hx-ask-rows.txt`

Expected: all pass, covering:
- opened → console and queue;
- an agent ask → a wake;
- answered → a wake plus a clear;
- withdrawn with `parent_closed` → a quiet clear.

- [ ] **Step 4: Write `docs/checks/2026-10-11-live-dry-run.md`**

The record holds:
- the output of `trk --version --json` (cite its `commit`);
- the feed `head` and `epoch` from `trk changes --since 0 --json | jq '{head,epoch}'`;
- the dry-run output from Step 2;
- the test summary from Step 3.

Commit it:

```bash
git add docs/checks/2026-10-11-live-dry-run.md
git commit -m "checks: live read-only dry run and ask-row test evidence (tr-yh17a)"
```

- [ ] **Step 5: Claim tr-yh17a and attach the evidence**

Use your own Claude session id as `$SESSION`.

```bash
trk claim tr-yh17a --as worker --session "$SESSION"
trk evidence add tr-yh17a --as worker --session "$SESSION" --kind test-run --gate built \
  --criterion ac-2 --verdict pass --sha "$(git -C ~/projects/herdr-extension rev-parse --short HEAD)" \
  --ref ~/projects/herdr-extension/docs/checks/2026-10-11-live-dry-run.md \
  --note "hx routes ask.opened/readdressed, wakes on answered/withdrawn, clears parent_closed; live dry run clean"
```

Expected: both commands exit 0.

- [ ] **Step 6: Hand the close to the tracker session**

Send the tracker session (`beads-alterantive`) a message with:
- the evidence id;
- the commit;
- the path of the check record.

Point out that tr-yh17a's `built` gate also needs a `pr`, and this repo is local with no PR. The tracker session decides how the gate is met, and closes tr-yh17a with its resolution section.

---

## Self-Review

1. **Spec coverage, slice 1:**
   - The tracker reader and contract validation: Task 1.
   - The (epoch, seq) cursor, rebuilds and quiet migration rows: Task 3.
   - Open asks as the truth, with keyed delivery: Tasks 5 and 7.
   - The agent fallback order, idle-only wakes and batching: Task 5.
   - Human console and queue sent at once: Tasks 5 and 6.
   - Answered and withdrawn wakes, with a quiet `parent_closed`: Task 5.
   - One idempotent pass and a dry run: Task 7.
   - `hx watch` with backoff, plus systemd: Task 8.
   - The live check for tr-yh17a: Task 9.
   - Deferred, as the spec's build order says: Spaces, the sidebar ⚑ count, `hx start`/`hx worker`, the lease-renewal hook, the restart cap, and the cutover. `state.bindings` exists but stays empty, so routing step 2 falls through until slice 2.
2. **Placeholder scan:** every code step has its code. Task 9's commands are exact, and the evidence flags match `trk evidence add --help`.
3. **Type consistency:**
   - `AskInfo(session, gen, title, question)` is the same in Tasks 2, 3 and 5.
   - `Notify`, `Wake` and `Clear` fields match between Tasks 5, 6 and 7.
   - `Pane(pane_id, workspace_id, session, status, cwd)` is the same in Tasks 4, 5 and 7.
   - `route(...)` takes the same parameter order in Tasks 5 and 7.
4. **Review Focus:** each of the five lines has its test in the owning task:
   - Task 6 (queue without a pane, plus Task 5 `test_queue_waits_for_a_live_pane`);
   - Task 5 (wake held while working);
   - Task 7 (second pass, trk failure);
   - Task 3 (new epoch).
