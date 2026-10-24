"""$XDG_CONFIG_HOME (default ~/.config)/hx/config.toml: who hears asks, which model each role runs, how Spaces start."""
import getpass
import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

MODEL = "claude-sonnet-5-5"
COMMANDS = {"orchestrator": "claude --model {model} '/hx-orchestrate {root}'",
            "solo": "{claude} --model '{{model}}' --effort {{effort}} -w {{worktree}} '/hx-work {{root}}'"}


@dataclass(frozen=True)
class Config:
    humans: dict
    models: dict
    commands: dict
    close_delay_s: int = 600
    trk_contract: str | None = None
    finish: str = "act"          # [worktrees] finish: act, report or off (finish.py)


def moved(new: Path, old: Path) -> Path:
    """`new`, after moving `old` there once: hx was herdr-extension, and a folder left under the old
    name moves on first use. When both exist, the new one wins and the old one stays as it is."""
    if not new.exists() and old.is_dir():
        try:
            old.rename(new)
        except FileNotFoundError:          # another hx process moved it first
            pass
    return new


def config_path() -> Path:
    if "HX_CONFIG" in os.environ:
        return Path(os.environ["HX_CONFIG"])
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return moved(base / "hx", base / "herdr-extension") / "config.toml"


def load_humans(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {getpass.getuser(): ["console"]}
    raw = tomllib.loads(path.read_text())
    return {who: list(v.get("channels", ["console"])) for who, v in raw.get("humans", {}).items()}


def _default_commands() -> dict:
    """Solo sessions run under claude-worker (an auto-compact backstop) where it is installed."""
    claude = "claude-worker" if shutil.which("claude-worker") else "claude"
    return {**COMMANDS, "solo": COMMANDS["solo"].format(claude=claude)}


def load_config(path: Path) -> Config:
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    contract = raw.get("trk", {}).get("contract")
    return Config(humans=load_humans(path),
                  models={r: raw.get("models", {}).get(r, MODEL) for r in ("orchestrator", "worker", "solo")},
                  commands={**_default_commands(), **raw.get("commands", {})},
                  close_delay_s=int(raw.get("spaces", {}).get("close_delay_s", 600)),
                  trk_contract=os.path.expanduser(contract) if contract else None,
                  finish=_finish_mode(raw.get("worktrees", {}).get("finish", "act")))


def _finish_mode(v) -> str:
    if v not in ("act", "report", "off"):
        raise ValueError(f"[worktrees] finish must be act, report or off, not {v!r}")
    return v
