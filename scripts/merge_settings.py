#!/usr/bin/env python3
"""Add or remove hx's Claude Code hook entries in a settings.json, touching nothing else.

    merge_settings.py add|remove --settings PATH --hook EVENT MATCHER COMMAND [--hook ...] [--dry-run]

MATCHER may be "" for an event that takes none (SessionStart). An entry whose command is already
present is left alone, so `add` twice changes nothing. `remove` deletes only hooks with exactly the
given commands. When the file changes, the old file is copied to <settings>.hx-bak-<UTC time>
first and the diff is printed. Stdlib only; the same rules as trk's scripts/merge_settings.py.
"""

import argparse
import difflib
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path


def commands(entry):
    return {h.get("command") for h in entry.get("hooks", [])}


def add(settings, wanted):
    hooks = settings.setdefault("hooks", {})
    for event, matcher, command in wanted:
        have = hooks.setdefault(event, [])
        if any(command in commands(e) for e in have):
            continue
        entry = {"matcher": matcher} if matcher else {}
        have.append({**entry, "hooks": [{"type": "command", "command": command}]})


def remove(settings, wanted):
    ours = {command for _, _, command in wanted}
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        kept = []
        for entry in hooks[event]:
            rest = [h for h in entry.get("hooks", []) if h.get("command") not in ours]
            if rest:
                kept.append({**entry, "hooks": rest})
            elif not entry.get("hooks"):
                kept.append(entry)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if "hooks" in settings and not settings["hooks"]:
        del settings["hooks"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("add", "remove"))
    ap.add_argument("--settings", required=True, type=Path)
    ap.add_argument("--hook", nargs=3, action="append", required=True, metavar=("EVENT", "MATCHER", "COMMAND"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    path = args.settings
    before = path.read_text() if path.exists() else ""
    try:
        settings = json.loads(before) if before.strip() else {}
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} is not valid JSON ({exc}); fix it and run again. Nothing was changed.")
    if not isinstance(settings, dict):
        sys.exit(f"{path} does not hold a JSON object. Nothing was changed.")
    (add if args.action == "add" else remove)(settings, args.hook)
    after = json.dumps(settings, indent=2) + "\n"
    if before.strip() and json.loads(before) == settings or not before.strip() and not settings:
        print(f"settings: {path} already up to date")
        return 0
    diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"{path} (old)", f"{path} (new)"))
    print(diff, end="")
    if args.dry_run:
        print(f"settings: would change {path}")
        return 0
    if path.exists():
        backup = path.with_name(f"{path.name}.hx-bak-{datetime.now(UTC):%Y%m%dT%H%M%SZ}")
        shutil.copy2(path, backup)
        print(f"settings: backed up to {backup}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(after)
    print(f"settings: updated {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
