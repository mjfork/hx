"""`hx sidebar setup`: point three keys of herdr's config.toml at the needs-you sidebar.

  [ui] sidebar_width            wide enough for a row (sidebar.py)
  [ui] tab_bar_right            `hx needs-you --line`, in place of herdr-projects' count
  [ui.sidebar.agents] rows      the hx tokens

Each key is one line, replaced in place or added; every other line stays as it is. The result
must parse to the old config plus exactly these three values, or nothing is written.
"""
import copy
import json
import os
import re
import tomllib
from pathlib import Path

WIDTH = 34
ROWS = [["state_icon", "$hx_item", {"token": "$hx_cache", "rules": [{"contains": "!", "bold": True}]},
         "$hx_score"],
        [{"token": "workspace", "dim": True}]]
HEADER = re.compile(r"^\s*(\[\[?)\s*([^\[\]]+?)\s*\]\]?\s*(#.*)?$")


class ConfigError(Exception):
    pass


def tab_bar(hx_bin: str) -> list[dict]:
    return [{"type": "command", "command": f"{hx_bin} needs-you --line",
             "interval_seconds": 15, "timeout_seconds": 5}]


def _toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)     # a JSON string is a TOML basic string
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    return "{ " + ", ".join(f"{k} = {_toml(x)}" for k, x in v.items()) + " }"


def _targets(hx_bin: str) -> list[tuple[str, str, object]]:
    return [("ui", "sidebar_width", WIDTH), ("ui", "tab_bar_right", tab_bar(hx_bin)),
            ("ui.sidebar.agents", "rows", ROWS)]


def _set(lines: list[str], section: str, key: str, value) -> list[str]:
    out = list(lines)
    new = f"{key} = {_toml(value)}"
    start = next((i for i, l in enumerate(out)
                  if (m := HEADER.match(l)) and m[1] == "[" and m[2] == section), None)
    if start is None:
        if out and out[-1].strip():
            out.append("")
        return out + [f"[{section}]", new]
    end = next((i for i in range(start + 1, len(out)) if HEADER.match(out[i])), len(out))
    at = next((i for i in range(start + 1, end) if re.match(rf"^\s*{re.escape(key)}\s*=", out[i])), None)
    if at is None:
        out.insert(start + 1, new)
    else:
        out[at] = new
    return out


def _strip(cfg: dict, targets) -> dict:
    cfg = copy.deepcopy(cfg)
    for section, key, _ in targets:
        node = cfg
        for part in section.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        if isinstance(node, dict):
            node.pop(key, None)
    for section in ("ui.sidebar.agents", "ui.sidebar", "ui"):     # sections this edit may have added
        parts = section.split(".")
        node = cfg
        for part in parts[:-1]:
            node = node.get(part, {})
        if node.get(parts[-1]) == {}:
            node.pop(parts[-1])
    return cfg


def edit(text: str, hx_bin: str) -> str:
    try:
        old = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"herdr's config does not parse ({e}); fix it first") from None
    targets = _targets(hx_bin)
    lines = text.splitlines()
    for section, key, value in targets:
        lines = _set(lines, section, key, value)
    new = "\n".join(lines) + "\n"
    try:
        got = tomllib.loads(new)
    except tomllib.TOMLDecodeError:
        got = None
    ok = got is not None and _strip(got, targets) == _strip(old, targets)
    for section, key, value in targets:
        node = got or {}
        for part in section.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        ok = ok and isinstance(node, dict) and node.get(key) == value
    if not ok:
        raise ConfigError("cannot place [ui] sidebar_width, tab_bar_right and [ui.sidebar.agents] rows "
                          "one line each (a value over several lines, or a dotted key?); change them by hand")
    return new if new != text else text


def apply(path: Path, hx_bin: str, stamp: str) -> Path | None:
    """Write the edit after a backup. Returns the backup, or None when nothing changed."""
    text = path.read_text() if path.exists() else ""
    new = edit(text, hx_bin)
    if new == text:
        return None
    backup = path.with_name(f"{path.name}.bak-hx-{stamp}")
    backup.write_text(text)
    tmp = path.with_name(f".{path.name}.hx.tmp")
    tmp.write_text(new)
    os.replace(tmp, path)
    return backup
