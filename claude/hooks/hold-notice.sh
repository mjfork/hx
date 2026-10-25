#!/usr/bin/env bash
# Claude Code SessionStart hook: a session that starts during a machine-wide hold is told so.
# It reads hx's hold.json by the contract's own rules and needs no hx install: held when the file
# has every key and until is null or a zoned time later than now; anything else counts as held.
# Prints nothing when there is no hold, never waits more than 5 seconds, and always exits 0.
f=${HX_STATE_DIR:-$HOME/.local/state/hx}/hold.json
[[ -e ${f%/*} ]] || exit 0                                # no state folder: hx never ran, no hold
unreadable="HOLD: $f cannot be read, so the machine counts as held. Start no new sessions, builds, tests or deploys; ask who holds it."
limit=(); command -v timeout >/dev/null && limit=(timeout 5)
read -r -d '' check <<'PY'
import json, os, re, stat, sys
from datetime import datetime, timezone
TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
try:
    fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NONBLOCK)  # never waits on a FIFO swapped in
except FileNotFoundError:
    if os.path.lexists(sys.argv[1]):                      # a link to nothing
        sys.exit(1)
    print("FREE")
    sys.exit(0)
if not stat.S_ISREG(os.fstat(fd).st_mode):
    sys.exit(1)
data = b""
while len(data) <= 65536:                                 # hx.hold.MAX_BYTES; read(2) may return early
    chunk = os.read(fd, 65537 - len(data))
    if not chunk:
        break
    data += chunk
if len(data) > 65536:
    sys.exit(1)
r = json.loads(data.decode("utf-8"))
if not isinstance(r, dict) or not {"id", "reason", "by", "since", "until"} <= r.keys():
    sys.exit(1)
until = r["until"]
if until is not None:
    if not isinstance(until, str) or not TIME.match(until):
        sys.exit(1)
    # a trailing Z as +00:00: python3 before 3.11 (macOS ships 3.9) cannot parse Z
    if datetime.fromisoformat(re.sub(r"Z$", "+00:00", until)).timestamp() <= datetime.now(timezone.utc).timestamp():
        print("FREE")
        sys.exit(0)
print(f"HOLD (hx {r['id']}): a machine-wide hold by {r['by']} since {r['since']}, "
      f"until {until or 'hx resume'}: {r['reason']} "
      "Start no new sessions, builds, tests or deploys until it ends. `hx held` says whether it still holds.")
PY
# -I -S: no user site, sitecustomize or PYTHON* variables can slow it down or print into the session
out=$("${limit[@]}" python3 -I -S -c "$check" "$f" 2>/dev/null)
case $? in
  0) [[ $out == FREE ]] || echo "$out" ;;
  *) echo "$unreadable" ;;
esac
exit 0
