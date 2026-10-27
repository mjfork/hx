#!/usr/bin/env bash
# Claude Code PreToolUse hook on Bash: a memory gate for heavy commands (tests, type-checks, builds).
# Many sessions testing at once can leave a box swap-exhausted for hours. While MemAvailable is
# under MEM_GATE_MB (default 16384), or MEM_GATE_MAX_PROCS (default 20) test processes already run,
# it denies the command and tells the session to wait and retry. Fails open on any error.
# Heavy means jest, vitest, tsc or playwright in command position, or npm test / type-check / build;
# MEM_GATE_PATTERN (an extended regex matched against the command) replaces that list.
# Disable: remove its entry from ~/.claude/settings.json hooks.PreToolUse, or export MEM_GATE_OFF=1.
# MEM_GATE_MEMINFO (default /proc/meminfo) is for tests. hx's install.sh adds it (on Linux).

[ -n "$MEM_GATE_OFF" ] && exit 0
MIN_MB=${MEM_GATE_MB:-16384}
MAX_PROCS=${MEM_GATE_MAX_PROCS:-20}

input=$(cat) || exit 0
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null) || exit 0
[ -z "$cmd" ] && exit 0

# Command position only: start of line, after ; & | ( or env assignments, after npx, or a node_modules/.bin path.
pos='(^|[;&|(] *)([A-Za-z_]+=[^ ]* +)*'
heavy="${MEM_GATE_PATTERN:-${pos}(npx +|[^ ]*node_modules/\.bin/)?(jest|vitest|tsc|playwright)( |$)|${pos}npm +(run +)?(test|type-check|typecheck|build|test:[a-z:-]+)( |$)}"
printf '%s' "$cmd" | grep -qE "$heavy" || exit 0

avail_mb=$(awk '/^MemAvailable:/{print int($2/1024)}' "${MEM_GATE_MEMINFO:-/proc/meminfo}" 2>/dev/null)
[ -z "$avail_mb" ] && exit 0
# Count node processes only: the bash -c, sh -c and npm exec wrappers around a run, and any monitor or
# loop script whose command line merely mentions a runner, are not test processes.
# (argv[0], not comm: node renames its thread to MainThread.)
procs=$(ps -eo args= 2>/dev/null | awk '$1 ~ /(^|\/)node$/ && /jest-worker|\/jest( |$)|\/bin\/jest|\/tsc( |$)|vitest|playwright/' | wc -l)
[ -z "$procs" ] && procs=0

reason=""
if [ "$avail_mb" -lt "$MIN_MB" ]; then
  reason="Memory gate: only ${avail_mb} MB available (gate is ${MIN_MB} MB). Other sessions are testing."
elif [ "$procs" -ge "$MAX_PROCS" ]; then
  reason="Memory gate: ${procs} test/type-check processes already running (gate is ${MAX_PROCS})."
fi
[ -z "$reason" ] && exit 0

reason="$reason Wait about 2 minutes (use Monitor or move on to other work), then retry the same command. Don't work around this gate by renaming or wrapping the command."
jq -n --arg r "$reason" '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
exit 0
