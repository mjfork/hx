#!/usr/bin/env bash
# Claude Code SessionStart hook: tell the session, unprompted, whether its checkout is at the tip.
#
# Why: staleness fails silently. A checkout hundreds of commits behind its base builds and tests
# GREEN against old code, and a worktree cut from a stale local ref can predate the very fix its
# ticket is about. Neither shows unless something asks, and nobody remembers to ask.
#
# The base is BASE_BRANCH, else the remote's default branch (origin/HEAD), else develop, else main.
# Contract: emits JSON on stdout with hookSpecificOutput.additionalContext, or nothing.
# It must NEVER fail the session: every path exits 0. Install: hx's install.sh --freshness-hook.
set -uo pipefail

cd "${CLAUDE_PROJECT_DIR:-$PWD}" 2>/dev/null || exit 0
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

base="${BASE_BRANCH:-}"
if [ -z "$base" ]; then
  base=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null) && base=${base#origin/}
fi
for b in "$base" develop main; do
  [ -n "$b" ] && git show-ref --verify --quiet "refs/remotes/origin/$b" && { base=$b; break; }
done
git show-ref --verify --quiet "refs/remotes/origin/$base" || exit 0

# Fetch at most every 10 minutes: a session-start hook runs constantly on a busy box and the
# point is freshness, not hammering the remote. A time limit, where `timeout` exists, so a hung
# network cannot stall every new session: a missing answer degrades to the last known ref.
stamp="$(git rev-parse --git-dir)/.freshness-fetch"
mtime=$(stat -c %Y "$stamp" 2>/dev/null || stat -f %m "$stamp" 2>/dev/null || echo 0)
if [ ! -f "$stamp" ] || [ $(( $(date +%s) - mtime )) -gt 600 ]; then
  limit=(); command -v timeout >/dev/null && limit=(timeout 20)
  ${limit[@]+"${limit[@]}"} git fetch origin "$base" -q >/dev/null 2>&1   # bash 3.2: an empty array trips set -u
  touch "$stamp" 2>/dev/null
fi

behind=$(git rev-list --count "HEAD..origin/$base" 2>/dev/null) || exit 0
[ -n "$behind" ] || exit 0
[ "$behind" = "0" ] && exit 0   # at tip: say nothing, no noise

ahead=$(git rev-list --count "origin/$base..HEAD" 2>/dev/null || echo "?")
branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
root=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)")

msg="CHECKOUT IS STALE. ${root} (branch ${branch}) is ${behind} commits BEHIND origin/${base}, and ${ahead} ahead. \
Before building, testing, shipping, or concluding anything about what the code does: merge or rebase onto origin/${base}, \
or state explicitly that you are working against an older tree and why. \
Two traps this causes: (1) a build or test run from a stale tree checks old code and still reports GREEN; \
(2) reading your own working tree instead of origin/${base} makes you draw conclusions about code that has already changed. \
If you compare anything against a baseline, name the commit the baseline is at."

jq -cn --arg m "$msg" '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$m}}' 2>/dev/null || true
exit 0
