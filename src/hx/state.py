"""The watcher's own memory: cursor, ask bookkeeping, and which action keys are done."""
import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from hx.config import moved


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
    askers: dict[str, str | None] = field(default_factory=dict)      # ask -> the session that opened it; a reopen keeps it
    done: dict[str, str] = field(default_factory=dict)
    bindings: dict[str, dict] = field(default_factory=dict)
    pending: list[dict] = field(default_factory=list)   # closed ask rows whose wake is still owed
    replies: list[dict] = field(default_factory=list)   # ask.replied rows whose wake is still owed
    comments: list[dict] = field(default_factory=list)  # comment.routed rows whose wake is still owed
    last_pass_at: str | None = None   # end of the last finished pass; the history cutoff after a restore
    sessions: dict[str, list] = field(default_factory=dict)   # session -> [item, role] last written to sessions.jsonl
    seen: dict[str, str] = field(default_factory=dict)        # session -> pane, from the last pass's herdr snapshot
    absent: dict[str, float] = field(default_factory=dict)    # session -> when its pane first showed no session
    reported: dict[str, list] = field(default_factory=dict)   # "ws:<id>" / "pane:<id>" -> [token value, epoch s]
    drafts: dict[str, dict] = field(default_factory=dict)     # pane -> what hx last saw in its input box (drafts.py)
    sidebar: list[str] = field(default_factory=list)          # panes shown in the needs-you panel last pass (sidebar.py)
    places: dict[str, list] = field(default_factory=dict)     # live session -> folders its pane works in (finish.py)
    finish: dict[str, dict] = field(default_factory=dict)     # gone session -> {paths, since}: trees to finish (finish.py)
    hold: dict | None = None                                  # the machine-wide hold hx last saw (hold.py)
    turns: dict[str, dict] = field(default_factory=dict)      # live session -> {status, n turns ended} (done.py)
    finished: list[dict] = field(default_factory=list)        # [done] lines still owed to drivers (done.py)


def state_dir() -> Path:
    if "HX_STATE_DIR" in os.environ:
        d = Path(os.environ["HX_STATE_DIR"])
    else:
        d = moved(Path.home() / ".local/state/hx", Path.home() / ".local/state/herdr-extension")
    d.mkdir(parents=True, exist_ok=True)
    return d


def load(path: Path) -> State:
    if not path.exists():
        return State()
    raw = json.loads(path.read_text())
    raw["asks"] = {k: AskInfo(**v) for k, v in raw.get("asks", {}).items()}
    known = {f.name for f in fields(State)}
    return State(**{k: v for k, v in raw.items() if k in known})


def save(state: State, path: Path) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=1, sort_keys=True))
    os.replace(tmp, path)


@contextmanager
def locked(path: Path):
    """One writer at a time: the watcher's pass and `hx start` both hold this around load..save."""
    with open(path.with_name(path.name + ".lock"), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
