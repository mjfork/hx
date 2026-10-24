#!/usr/bin/env bash
# Say whether the box can take another Claude session right now; starts nothing.
#
#   launch-guard.sh [--check-only | --hold-only] <label>     e.g. launch-guard.sh tr-abc12
#
# Exit 0: ok to start (the launch time is recorded, so the next caller waits out the gap).
# Exit 3: held; one reason line on stderr. Exit 2: bad usage.
# Holds while the 1-min load is above DISPATCH_MAX_LOAD (default 2 x CPUs), the previous launch was
# under DISPATCH_GAP_SEC (default 30) ago, MemAvailable is under DISPATCH_MIN_MEM_MB (default 16384),
# swap is DISPATCH_MAX_SWAP_PCT (default 95) % used or more (no swap: skipped),
# or the root disk is DISPATCH_MAX_DISK_PCT (default 95) % full or more. DISPATCH_FORCE=1 skips the
# checks (the launch is still recorded). --check-only records nothing. DISPATCH_MEMINFO (default /proc/meminfo) is for tests.
# Machine-wide hold first: held while ~/.local/state/hx/hold.json (HERDR_HOLD_FILE) exists and its "until" is null or
# in the future; an unreadable file counts as held. DISPATCH_FORCE does not skip it; only HERDR_HOLD_OVERRIDE=1 does.
# --hold-only checks just the hold and swap, and records nothing (for wrappers that start sessions). Writer: hx hold / hx resume.
# Why: too many sessions starting at once starve Claude Code's hook workers, and it unloads mods.
# Callers: hx start, and any launcher that should wait its turn. Linux only (/proc, nproc, flock, GNU df).
set -euo pipefail

record=1; hold_only=0
[[ ${1:-} == --check-only ]] && { record=0; shift; }
[[ ${1:-} == --hold-only ]] && { record=0; hold_only=1; shift; }
[[ $# -eq 1 && -n $1 ]] || { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
label=$1

hold_file="${HERDR_HOLD_FILE:-$HOME/.local/state/hx/hold.json}"
if [[ "${HERDR_HOLD_OVERRIDE:-}" != 1 && ( -e $hold_file || -L $hold_file ) ]]; then
  # Exit 42 = the hold has expired (the only release); exit 0 prints the reason. Any other status (no python3, a crash,
  # a startup error, which exits 1) counts as held. -I ignores PYTHON* env and the cwd, so they can't break the parse.
  rc=0; why=$(python3 -I - "$hold_file" <<'PY'
import datetime, json, os, re, stat, sys
try:
    # Open without blocking and check the descriptor itself, so a FIFO swapped in can't hang the read.
    fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        print("hold file is not a regular file"); sys.exit(0)
    # Same cap as hx and its hook: anything over 64 KiB counts as held.
    data = b""
    while len(data) <= 65536:
        chunk = os.read(fd, 65537)
        if not chunk: break
        data += chunk
    if len(data) > 65536:
        print("hold file over 64 KiB"); sys.exit(0)
    d = json.loads(data.decode("utf-8"))  # strict UTF-8 as hx does; a BOM or UTF-16 is unreadable, so held
    if not isinstance(d, dict): raise ValueError
except Exception:
    print("hold file unreadable"); sys.exit(0)
if not {"id", "reason", "by", "since", "until"} <= d.keys():
    print("hold file missing keys"); sys.exit(0)
u = d.get("until")
if u is not None:
    # The contract is a full ISO-8601 timestamp with an explicit zone; anything else holds.
    if not (isinstance(u, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})", u)):
        print("hold 'until' unreadable"); sys.exit(0)
    try:
        t = datetime.datetime.fromisoformat(u.replace("Z", "+00:00"))
    except ValueError:
        print("hold 'until' unreadable"); sys.exit(0)
    if t <= datetime.datetime.now(datetime.timezone.utc): sys.exit(42)
print(f"{d.get('reason', 'no reason given')}; by {d.get('by', '?')} since {d.get('since', '?')}" + (f", until {u}" if u else ""))
PY
  ) || rc=$?
  if (( rc != 42 )); then
    (( rc == 0 )) || why="hold check failed (status $rc)"
    echo "launch-guard: machine-wide hold ($why); not starting $label. Wait for hx resume." >&2; exit 3
  fi
fi

# Swap runs in --hold-only too, so launches through a wrapper are covered; it records nothing,
# so running it twice (hx start, then a wrapper) is harmless.
swap_check() {
  local meminfo="${DISPATCH_MEMINFO:-/proc/meminfo}" maxswap="${DISPATCH_MAX_SWAP_PCT:-95}" swap
  swap=$(awk '/^SwapTotal:/ {t=$2} /^SwapFree:/ {f=$2} END {if (t > 0) print int((t-f)*100/t); else print -1}' "$meminfo")
  if (( swap >= maxswap )); then
    echo "launch-guard: swap ${swap}% >= ${maxswap}%; not starting $label. Retry later." >&2; exit 3
  fi
}
if (( hold_only )); then [[ -z "${DISPATCH_FORCE:-}" ]] && swap_check; exit 0; fi

stamp="${XDG_STATE_HOME:-$HOME/.local/state}/launch-guard.last"
mkdir -p "${stamp%/*}"
exec 9>"$stamp.lock"
flock -w 10 9 || { echo "launch-guard: busy; not starting $label. Retry." >&2; exit 3; }

if [[ -z "${DISPATCH_FORCE:-}" ]]; then
  max="${DISPATCH_MAX_LOAD:-$(( $(nproc) * 2 ))}"; gap="${DISPATCH_GAP_SEC:-30}"
  read -r load1 _ < /proc/loadavg
  if awk -v l="$load1" -v m="$max" 'BEGIN { exit !(l > m) }'; then
    echo "launch-guard: load $load1 is above $max; not starting $label. Retry later (DISPATCH_FORCE=1 overrides)." >&2; exit 3
  fi
  minmem="${DISPATCH_MIN_MEM_MB:-16384}"; maxdisk="${DISPATCH_MAX_DISK_PCT:-95}"
  meminfo="${DISPATCH_MEMINFO:-/proc/meminfo}"
  mem=$(awk '/^MemAvailable:/ {print int($2/1024)}' "$meminfo")
  if (( mem < minmem )); then
    echo "launch-guard: only ${mem} MB memory available (need ${minmem}); not starting $label. Retry later." >&2; exit 3
  fi
  swap_check
  disk=$(df --output=pcent / | tail -1 | tr -dc 0-9)
  if (( disk >= maxdisk )); then
    echo "launch-guard: root disk ${disk}% full (limit ${maxdisk}%); not starting $label. Free space first." >&2; exit 3
  fi
  last=$(cat "$stamp" 2>/dev/null || echo 0); since=$(( $(date +%s) - last ))
  if (( since < gap )); then
    echo "launch-guard: last launch was ${since}s ago (gap ${gap}s); not starting $label. Retry in $(( gap - since ))s." >&2; exit 3
  fi
fi

(( record )) && date +%s > "$stamp"
exit 0
