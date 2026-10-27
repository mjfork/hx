#!/usr/bin/env bash
# Install hx from this checkout, or remove it again. Safe to re-run.
#
#   ./install.sh [--dry-run] [--no-editable] [--no-service] [--no-sidebar] [--force] [--me NAME]
#                [--no-launch-guard] [--no-claude-worker] [--no-mem-gate] [--no-freshness-hook]
#   ./install.sh --uninstall [--dry-run]
#   ./install.sh make-bundle [FILE]
#
# See `./install.sh --help` and README.md.
set -euo pipefail
shopt -u patsub_replacement 2>/dev/null || true   # bash 5.2: keep & in a path literal in ${x//a/b}

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
SKILLS=(hx-work)
UNIT=hx.service
GUARD=launch-guard.sh   # the launch guard and claude-worker go on PATH, where hx start finds them
ALL_BINS=($GUARD claude-worker)
OLD=herdr-extension      # hx's name before the rename: its folders, unit and uv tool move over

DRY=0 UNINSTALL=0 EDITABLE=1 FORCE=0 SERVICE=1 SIDEBAR=1 ME=${USER:-$(id -un)} MODE=install BUNDLE=
# The extras are on by default; the launch guard and the memory gate read /proc, so only on Linux.
# ASKED: the ones named on the command line, which refuse a differing script instead of skipping it.
LINUX=0; [[ $(uname -s) == Linux ]] && LINUX=1
LAUNCH_GUARD=$LINUX CLAUDE_WORKER=1 MEM_GATE=$LINUX FRESHNESS=1 ASKED=:

usage() {
  cat <<'EOF'
Usage: install.sh [options]
       install.sh make-bundle [FILE]

Installs hx from this checkout: the `hx` command (with uv), the hx-work Claude Code skill, a
starter config if none exists, the needs-you list in herdr's Agents panel, and the watcher as a
systemd user service. It also installs four extras that keep many sessions cheap and safe: a
worker wrapper, a launch guard and two Claude Code hooks. Each has a --no-* option to skip it.

Options:
  --dry-run       print what would happen and change nothing
  --uninstall     remove what install.sh added; config and state are never touched
  --no-editable   install a copy of hx (and the skill), not links to this checkout
  --no-service    do not install or start the watcher service (systemd; launchd on macOS)
  --no-sidebar    leave herdr's config.toml alone: no needs-you list in the Agents panel
  --force         reinstall the hx command even if uv already has it, and replace a skill that
                  differs from the repo copy (the old one is kept as ~/.local/share/hx-skill-backups/)
  --me NAME       the trk actor who hears asks, for a new config (default: your user name)
  --no-claude-worker
                  skip claude-worker, which goes on PATH (~/.local/bin): solo sessions run under it
                  and auto-compact at 250k tokens (CLAUDE_CODE_AUTO_COMPACT_WINDOW), re-sending
                  less context each turn
  --no-launch-guard
                  skip launch-guard.sh, which goes on PATH: hx start waits while the box is held,
                  loaded, short of memory, swap or disk, or launched a session under 30s ago
                  (Linux only; skipped elsewhere unless --launch-guard asks for it)
  --no-mem-gate   skip the mem-gate hook in ~/.claude/settings.json: heavy test and build
                  commands wait while memory is short or many test processes run (Linux only)
  --no-freshness-hook
                  skip the checkout-freshness hook in ~/.claude/settings.json: a session whose
                  checkout is behind its base branch is told so when it starts
  --launch-guard, --claude-worker, --mem-gate, --freshness-hook
                  install that extra even where it is skipped by default, and refuse (without
                  --force) to replace a differing copy instead of leaving it alone
  -h, --help      show this help

make-bundle [FILE]  write the whole repo, with its history, to one file (default
                    ./hx.bundle). Copy it to another machine and run
                    `git clone hx.bundle hx`.

Needs: python 3.12+, uv, trk and herdr. Claude Code runs the sessions hx starts.
Environment: HOME and XDG_CONFIG_HOME decide where the skill, config and unit go.
EOF
}

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
# Print a command in dry-run mode, run it otherwise.
act() {
  if ((DRY)); then say "[dry-run] $*"; else "$@"; fi
}

while (($#)); do
  case $1 in
    --dry-run) DRY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --no-editable) EDITABLE=0 ;;
    --no-service) SERVICE=0 ;;
    --no-sidebar) SIDEBAR=0 ;;
    --force) FORCE=1 ;;
    --launch-guard) LAUNCH_GUARD=1 ASKED+=$GUARD: ;;
    --claude-worker) CLAUDE_WORKER=1 ASKED+=claude-worker: ;;
    --mem-gate) MEM_GATE=1 ;;
    --freshness-hook) FRESHNESS=1 ;;
    --no-launch-guard) LAUNCH_GUARD=0 ;;
    --no-claude-worker) CLAUDE_WORKER=0 ;;
    --no-mem-gate) MEM_GATE=0 ;;
    --no-freshness-hook) FRESHNESS=0 ;;
    --me) shift; [[ $# -gt 0 ]] || die "--me needs a name"; ME=$1 ;;
    make-bundle) MODE=bundle; if [[ ${2:-} != "" && ${2:-} != -* ]]; then BUNDLE=$2; shift; fi ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
  shift
done


CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
CLAUDE_DIR=$HOME/.claude
# Replaced skills are kept outside every folder Claude Code loads skills from, so a backup never loads as a skill.
SKILL_BACKUPS=${XDG_DATA_HOME:-$HOME/.local/share}/hx-skill-backups
SETTINGS=$CLAUDE_DIR/settings.json
BIN_DIR=$HOME/.local/bin
UNIT_DIR=$CONFIG_HOME/systemd/user
AGENT=$HOME/Library/LaunchAgents/com.hx.watch.plist     # the watcher on macOS, where there is no systemctl
STATE_HOME=${XDG_STATE_HOME:-$HOME/.local/state}
CONFIG=${HX_CONFIG:-$CONFIG_HOME/hx/config.toml}   # where hx itself looks
# Written only when this script installs the uv tool, so --uninstall removes only what it added.
MARKS=$STATE_HOME/hx-install
MARKER=$MARKS/tool-installed
[[ $CONFIG == /* ]] || CONFIG=$PWD/$CONFIG     # the service starts elsewhere: name it absolutely

make_bundle() {
  local out=${BUNDLE:-hx.bundle}
  command -v git >/dev/null || die "git is required for make-bundle"
  [[ -e $out ]] && die "$out already exists; remove it or pick another name"
  act git -C "$REPO" bundle create "$(cd "$(dirname "$out")" && pwd -P)/$(basename "$out")" --all
  ((DRY)) || say "On the other machine: git clone $out hx && cd hx && ./install.sh"
}

skill_state() { # linked | same | differs | dangling | absent for one skill
  local src=$REPO/claude/skills/$1 dest=$CLAUDE_DIR/skills/$1
  if [[ ! -e $dest && ! -L $dest ]]; then say absent
  elif [[ -L $dest && ! -e $dest ]]; then say dangling     # a link into a moved or deleted checkout
  elif [[ -L $dest && $(cd "$dest" 2>/dev/null && pwd -P) == "$src" ]]; then say linked
  elif [[ ! -L $dest ]] && diff -r "$src" "$dest" >/dev/null 2>&1; then say same
  else say differs; fi
}

check_skills() { # refuse to replace a skill that differs from the repo copy, unless --force
  local s differing=()
  for s in ${SKILLS[@]+"${SKILLS[@]}"}; do [[ $(skill_state "$s") == differs ]] && differing+=("$s"); done
  ((${#differing[@]})) || return 0
  say "== skills that differ from the repo copy: ${differing[*]}"
  if ((FORCE)); then
    say "--force given: they will be replaced; the old copies are kept in $SKILL_BACKUPS"
  elif ((DRY)); then
    say "[dry-run] a real run would refuse here; add --force to replace them"
  else
    die "the skills above differ from the repo copy (local edits, an older install, or a link elsewhere). Nothing was changed. Run again with --force to replace them."
  fi
}

put_skill() { # and remember that this script put it there, so --uninstall removes only its own
  if ((EDITABLE)); then act ln -s "$REPO/claude/skills/$1" "$CLAUDE_DIR/skills/$1"
  else act cp -R "$REPO/claude/skills/$1" "$CLAUDE_DIR/skills/$1"; fi
  # the mark holds the copy's hash: an unedited copy stays ours after the checkout moves on
  ((DRY)) || { mkdir -p "$MARKS" && { ((EDITABLE)) && : || tree_hash "$CLAUDE_DIR/skills/$1"; } >"$MARKS/skill-$1"; }
}

SHA=(sha256sum); command -v sha256sum >/dev/null || SHA=(shasum -a 256)   # macOS has only shasum
tree_hash() { (cd "$1" && find . -type f -print0 | sort -z | xargs -0 "${SHA[@]}" | "${SHA[@]}" | cut -d' ' -f1); }

install_skills() {
  say "== skills"
  local s stamp
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  act mkdir -p "$CLAUDE_DIR/skills"
  for s in ${SKILLS[@]+"${SKILLS[@]}"}; do
    case $(skill_state "$s") in
      linked) if ((EDITABLE)); then say "skill $s: already installed (linked to this checkout)"
        else say "skill $s: replacing the link with a copy"; act rm "$CLAUDE_DIR/skills/$s"; put_skill "$s"; fi ;;
      same) if ((EDITABLE)); then say "skill $s: replacing the copy with a link to this checkout"
          act rm -rf "$CLAUDE_DIR/skills/$s"; put_skill "$s"
        else say "skill $s: already installed"; fi ;;
      absent) say "skill $s: installing"; put_skill "$s" ;;
      dangling) say "skill $s: replacing a link to a folder that no longer exists"
        act rm "$CLAUDE_DIR/skills/$s"; put_skill "$s" ;;
      differs) say "skill $s: replacing; keeping the old one as $SKILL_BACKUPS/$s.$stamp"
        act mkdir -p "$SKILL_BACKUPS"
        act mv "$CLAUDE_DIR/skills/$s" "$SKILL_BACKUPS/$s.$stamp"
        put_skill "$s" ;;
    esac
  done
}

uninstall_skills() {
  say "== skills"
  local s
  for s in ${SKILLS[@]+"${SKILLS[@]}"}; do
    local dest=$CLAUDE_DIR/skills/$s state mine=0
    state=$(skill_state "$s")
    if [[ -e $MARKS/skill-$s ]]; then
      case $state in
        linked | same | dangling) mine=1 ;;
        differs) [[ ! -L $dest && $(tree_hash "$dest") == "$(cat "$MARKS/skill-$s")" ]] && mine=1 ;;
      esac
    fi
    if [[ $state == absent ]]; then say "skill $s: not installed"
    elif ((mine)); then say "skill $s: removing"; act rm -rf "$dest"; act rm -f "$MARKS/skill-$s"
    elif [[ -e $MARKS/skill-$s ]]; then say "skill $s: edited since install; leaving it alone"
    else say "skill $s: not installed by this script; leaving it alone"; fi
  done
}

bin_state() { # linked | same | differs | dangling | absent for one of scripts/ on PATH
  local src=$REPO/scripts/$1 dest=$BIN_DIR/$1
  if [[ ! -e $dest && ! -L $dest ]]; then say absent
  elif [[ -L $dest && ! -e $dest ]]; then say dangling
  elif [[ -L $dest && $(readlink -f "$dest") == "$src" ]]; then say linked
  elif [[ ! -L $dest ]] && cmp -s "$src" "$dest"; then say same
  else say differs; fi
}

ours() { # this script put the script there and it is unedited: an older release, safe to replace
  local dest=$BIN_DIR/$1
  [[ -e $MARKS/bin-$1 ]] || return 1
  [[ -L $dest ]] || [[ $("${SHA[@]}" <"$dest" | cut -d' ' -f1) == "$(cat "$MARKS/bin-$1")" ]]
}

check_bins() { # an earlier release of ours is replaced; anyone else's copy is refused or left alone, unless --force
  local b k differing=() keep=()
  for b in ${BINS[@]+"${BINS[@]}"}; do
    [[ $(bin_state "$b") == differs ]] && ! ours "$b" && differing+=("$b")
  done
  ((${#differing[@]})) || return 0
  say "== scripts in $BIN_DIR that differ from the repo copy: ${differing[*]}"
  if ((FORCE)); then
    say "--force given: they will be replaced; the old ones are kept as <name>.bak-<time>"
    return 0
  fi
  for b in ${differing[@]+"${differing[@]}"}; do
    [[ $ASKED == *:$b:* ]] && continue
    warn "$b: not ours (your own, local edits, or a link elsewhere); leaving it alone. Run again with --force to replace it."
    for k in ${BINS[@]+"${BINS[@]}"}; do [[ $k == "$b" ]] || keep+=("$k"); done
    BINS=(${keep[@]+"${keep[@]}"}) keep=()
  done
  for b in ${differing[@]+"${differing[@]}"}; do
    [[ $ASKED == *:$b:* ]] || continue
    if ((DRY)); then say "[dry-run] a real run would refuse here; add --force to replace them"; return 0; fi
    die "the scripts above differ from the repo copy (your own, local edits, or a link elsewhere). Nothing was changed. Run again with --force to replace them."
  done
}

put_bin() { # and remember that this script put it there, so --uninstall removes only its own
  if ((EDITABLE)); then act ln -s "$REPO/scripts/$1" "$BIN_DIR/$1"
  else act cp -p "$REPO/scripts/$1" "$BIN_DIR/$1"; fi
  ((DRY)) || { mkdir -p "$MARKS" && { ((EDITABLE)) && : || "${SHA[@]}" <"$BIN_DIR/$1" | cut -d' ' -f1; } >"$MARKS/bin-$1"; }
}

install_bins() {
  ((${#BINS[@]})) || return 0
  say "== scripts"
  local b dest stamp
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  act mkdir -p "$BIN_DIR"
  for b in ${BINS[@]+"${BINS[@]}"}; do
    dest=$BIN_DIR/$b
    case $(bin_state "$b") in
      linked) if ((EDITABLE)); then say "$b: already installed (linked to this checkout)"
        else say "$b: replacing the link with a copy"; act rm "$dest"; put_bin "$b"; fi ;;
      same) if ((EDITABLE)); then say "$b: replacing the copy with a link to this checkout"
          act rm -f "$dest"; put_bin "$b"
        else say "$b: already installed"; fi ;;
      absent) say "$b: installing in $BIN_DIR"; put_bin "$b" ;;
      dangling) say "$b: replacing a link to a file that no longer exists"
        act rm "$dest"; put_bin "$b" ;;
      differs) say "$b: replacing; keeping the old one as $b.bak-$stamp"
        act mv "$dest" "$dest.bak-$stamp"; put_bin "$b" ;;
    esac
  done
  case :$PATH: in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not on PATH, so hx start will not find ${BINS[*]}" ;;
  esac
}

uninstall_bins() {
  local b dest state mine
  for b in ${ALL_BINS[@]+"${ALL_BINS[@]}"}; do
    dest=$BIN_DIR/$b mine=0
    state=$(bin_state "$b")
    [[ $state == absent && ! -e $MARKS/bin-$b ]] && continue      # never asked for: nothing to say
    say "== $b"
    if [[ -e $MARKS/bin-$b ]]; then
      case $state in
        linked | same | dangling) mine=1 ;;
        differs) [[ ! -L $dest && $("${SHA[@]}" <"$dest" | cut -d' ' -f1) == "$(cat "$MARKS/bin-$b")" ]] && mine=1 ;;
      esac
    fi
    if [[ $state == absent ]]; then say "$b: not installed"; act rm -f "$MARKS/bin-$b"
    elif ((mine)); then say "$b: removing"; act rm -f "$dest" "$MARKS/bin-$b"
    elif [[ -e $MARKS/bin-$b ]]; then say "$b: edited since install; leaving it alone"
    else say "$b: not installed by this script; leaving it alone"; fi
  done
}

# The hooks run from this checkout, as trk's do; --no-editable does not copy them.
BINS=()
((LAUNCH_GUARD)) && BINS+=("$GUARD")
((CLAUDE_WORKER)) && BINS+=(claude-worker)

MEM_HOOK=(--hook PreToolUse Bash "$REPO/claude/hooks/mem-gate.sh")
FRESH_HOOK=(--hook SessionStart "" "$REPO/claude/hooks/checkout-freshness.sh")

settings_cmd() { # add|remove, then --hook EVENT MATCHER COMMAND triples
  local flags=()
  ((DRY)) && flags+=(--dry-run)
  [[ -n $PY ]] || find_python || die "python 3.12 or newer is required"
  "$PY" "$REPO/scripts/merge_settings.py" "$@" --settings "$SETTINGS" ${flags[@]+"${flags[@]}"}
}

wanted_hooks() { # the --hook triples the options asked for, one word per line
  ((MEM_GATE)) && printf '%s\n' ${MEM_HOOK[@]+"${MEM_HOOK[@]}"}
  ((FRESHNESS)) && printf '%s\n' ${FRESH_HOOK[@]+"${FRESH_HOOK[@]}"}
  return 0
}

other_hook() { # settings.json already runs a script of this name from somewhere other than this checkout
  [[ -f $SETTINGS ]] || return 1
  "$PY" - "$SETTINGS" "$1" "$REPO" <<'PY'
import json, sys
try:
    hooks = json.load(open(sys.argv[1], encoding="utf-8")).get("hooks") or {}
except (OSError, ValueError, AttributeError):
    sys.exit(1)   # check_settings reports a bad file
name, repo = sys.argv[2], sys.argv[3]
cmds = [h.get("command", "") for es in hooks.values() for e in es for h in e.get("hooks", [])]
sys.exit(0 if any(name in c and repo not in c for c in cmds) else 1)
PY
}

skip_other_hooks() { # a box that runs its own copy of a hook keeps it: never two of the same hook
  [[ -n $PY ]] || find_python || return 0
  if ((MEM_GATE)) && other_hook mem-gate.sh; then
    warn "mem-gate: settings.json already runs a mem-gate.sh from elsewhere; leaving it alone"; MEM_GATE=0
  fi
  if ((FRESHNESS)) && other_hook checkout-freshness.sh; then
    warn "freshness hook: settings.json already runs a checkout-freshness.sh from elsewhere; leaving it alone"; FRESHNESS=0
  fi
  return 0
}

check_settings() { # before any change: settings.json can take the hooks asked for
  local hooks=() l
  while IFS= read -r l; do hooks+=("$l"); done < <(wanted_hooks)   # no mapfile: macOS has bash 3.2
  ((${#hooks[@]})) || return 0
  "$PY" "$REPO/scripts/merge_settings.py" add --settings "$SETTINGS" --dry-run ${hooks[@]+"${hooks[@]}"} >/dev/null ||
    die "$SETTINGS cannot take the hooks (see above); nothing was changed"
}

install_hooks() {
  local hooks=() l
  while IFS= read -r l; do hooks+=("$l"); done < <(wanted_hooks)   # no mapfile: macOS has bash 3.2
  ((${#hooks[@]})) || return 0
  say "== Claude Code hooks"
  command -v jq >/dev/null || warn "jq is not on PATH; the hooks need it, and without it they let everything through"
  settings_cmd add ${hooks[@]+"${hooks[@]}"}
}

uninstall_hooks() { # removes only hx's own entries, whichever options installed them
  [[ -e $SETTINGS ]] || return 0
  say "== Claude Code hooks"
  settings_cmd remove ${MEM_HOOK[@]+"${MEM_HOOK[@]}"} ${FRESH_HOOK[@]+"${FRESH_HOOK[@]}"} ||
    warn "$SETTINGS could not be read, so hx's hooks are still in it; remove them by hand"
}

check_paths() { # before any change: paths the unit will name must not need systemd quoting
  ((SERVICE)) || return 0
  [[ $CONFIG =~ [[:space:]\"\'\\%\$] ]] &&
    die "the config path '$CONFIG' has characters systemd would need quoted; pick another HX_CONFIG or use --no-service"
  local hx
  hx=$(hx_bin)
  [[ $hx =~ [[:space:]\"\'\\%\$] ]] &&
    die "the hx path '$hx' has characters systemd would need quoted; install uv tools somewhere simpler or use --no-service"
  return 0
}

check_folders() { # before any change: each folder hx will use is a folder or not there yet
  local d
  for d in "$(dirname "$CONFIG")" "$CONFIG_HOME/hx" "$STATE_HOME/hx" "$MARKS" "$CLAUDE_DIR/skills" "$UNIT_DIR"; do
    if [[ -L $d && ! -e $d ]] || [[ -e $d && ! -d $d ]]; then     # a dangling link is not a folder either
      die "$d exists and is not a folder; move it away and run again"
    fi
  done
  [[ -L $CONFIG && ! -e $CONFIG ]] && die "$CONFIG is a link to nothing; remove it or point it at your config, and run again"
  return 0
}

check_actor() { # before any change: a new config names $ME as a trk actor
  [[ -e $CONFIG ]] && return 0
  [[ -z ${HX_CONFIG:-} && -e $CONFIG_HOME/$OLD/config.toml && ! -e $CONFIG_HOME/hx ]] && return 0   # moves over
  # trk's actor grammar (contract/actors.schema.json); it also keeps the TOML table name plain.
  [[ $ME =~ ^[a-z0-9][a-z0-9-]*$ ]] ||
    die "'$ME' cannot be a trk actor (lowercase letters, digits and -, not starting with -); pick one with --me NAME"
}

# The first python 3.12+ on PATH: python3, else a versioned one (macOS puts its own 3.9 python3 first).
PY=
find_python() {
  local c
  for c in python3 python3.14 python3.13 python3.12; do
    command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null &&
      { PY=$c; return 0; }
  done
  return 1
}

check_prereqs() {
  say "== checks"
  find_python || die "python 3.12 or newer is required; found $(python3 --version 2>&1 || echo 'no python3 on PATH')"
  command -v uv >/dev/null || die "uv is required (https://docs.astral.sh/uv/); it is not on PATH"
  command -v trk >/dev/null || die "trk is required: hx reads its work and asks from trk. Install trk first (its own ./install.sh)."
  command -v herdr >/dev/null || die "herdr is required: hx opens Spaces and wakes panes in herdr. Install herdr first."
  command -v claude >/dev/null || warn "claude (Claude Code) is not on PATH; hx start launches sessions with it"
  if command -v claude-worker >/dev/null; then say "claude-worker found: solo sessions run under it"
  else say "claude-worker not found: solo sessions run under plain claude"; fi
  if grep -qs '"eng-toolkit@' "$CLAUDE_DIR/plugins/installed_plugins.json"; then
    say "eng-toolkit found: hx-work hands JIRA tickets' gates to it"
  else say "eng-toolkit not found: hx-work runs the trk gates itself"; fi
  say "$PY, uv, trk and herdr found"
}

tool_installed() { grep -q "^${1:-hx} " <<<"$(uv tool list 2>/dev/null)"; }   # no pipe: grep -q exits early, and pipefail would see uv fail

move_folder() { # herdr-extension's folder becomes hx's, once; a folder already under the new name wins
  local old=$1 new=$2
  [[ -d $old ]] || return 0
  if [[ -e $new ]]; then say "$new exists, so hx uses it; $old is left as it is"
  elif mv --version >/dev/null 2>&1; then say "moving $old to $new"; act mv -T "$old" "$new"   # -T: never into a new that just appeared
  else say "moving $old to $new"; act mv "$old" "$new"; fi   # macOS mv has no -T; $new was checked just above
}

replace_old_service() { # stop the herdr-extension unit, so one watcher runs, not two
  [[ -e $UNIT_DIR/$OLD.service ]] || return 0
  # the old marks folder moved to $MARKS, unless both existed or this is a dry run
  if ((SERVICE)) && [[ -e $STATE_HOME/$OLD-install/service || -e $MARKS/service ]]; then
    say "== old watcher service"
    say "stopping and removing $OLD.service; hx.service replaces it"
    if command -v systemctl >/dev/null || ((DRY)); then act systemctl --user disable --now "$OLD"; fi
    act rm -f "$UNIT_DIR/$OLD.service"
  else
    warn "$UNIT_DIR/$OLD.service is still installed and would run a second watcher. Stop it: systemctl --user disable --now $OLD"
  fi
}

install_tool() {
  say "== hx command"
  local args=(tool install)
  ((EDITABLE)) && args+=(--editable)
  if tool_installed "$OLD"; then
    # The old tool owns the hx executable. Take it over first, so a failed install leaves the old
    # tool and its watcher as they were; uv's uninstall then deletes the hx link, so link it again.
    # uv's own uninstall would delete the hx link with it, so the old tool's folder goes instead.
    say "replacing the $OLD uv tool with hx"
    act uv ${args[@]+"${args[@]}"} --force "$REPO"
    local tools
    tools=$(uv tool dir 2>/dev/null) || tools=
    if [[ -n $tools && -f $tools/$OLD/uv-receipt.toml ]]; then act rm -rf "${tools:?}/$OLD"
    else warn "the $OLD tool's folder was not found; hx works, and \`uv tool list\` still shows $OLD"; fi
    if ((DRY)); then say "[dry-run] record that this script installed hx"
    else mkdir -p "$(dirname "$MARKER")" && printf '%s\n' "$REPO" >"$MARKER"; fi
    return
  fi
  ((FORCE)) && args+=(--force)
  if tool_installed && ! ((FORCE)); then
    say "hx is already installed (use --force to reinstall)"
  else
    act uv ${args[@]+"${args[@]}"} "$REPO"
    if ((DRY)); then say "[dry-run] record that this script installed hx"
    else mkdir -p "$(dirname "$MARKER")" && printf '%s\n' "$REPO" >"$MARKER"; fi
  fi
}

trk_contract() { # the contract dir of an editable trk checkout, if trk is one
  local py
  py=$(head -1 "$(command -v trk)" 2>/dev/null | sed -n 's/^#!//p')
  [[ -n $py && -x $py ]] || return 0
  local dir
  dir=$("$py" -c 'import os, trk; print(os.path.dirname(os.path.dirname(os.path.dirname(trk.__file__))))' 2>/dev/null) || return 0
  [[ -d $dir/contract ]] && printf '%s\n' "$dir/contract"
  return 0
}

render_config() {
  local channels='"console"' contract
  [[ -d $HOME/.herdr-queue ]] && channels='"console", "queue"'
  contract=$(trk_contract)
  cat <<EOF
# hx config. See README.md, "Configuration".

# Who hears asks, by trk actor name, and on which channels: "console" (the hx inbox log) and
# "queue" (the herdr needs-you queue in ~/.herdr-queue).
[humans.$ME]
channels = [$channels]

# The model each role runs.
# [models]
# solo = "claude-sonnet-5-5"

# How long a Space stays open after its root closes and its pane is idle.
# [spaces]
# close_delay_s = 600

EOF
  if [[ -n $contract ]]; then
    printf '# Check trk replies against this trk checkout'"'"'s contract, not the copy hx ships.\n[trk]\ncontract = "%s"\n' "$contract"
  else
    printf '# Check trk replies against another contract than the copy hx ships.\n# [trk]\n# contract = "~/src/trk/contract"\n'
  fi
}

install_config() {
  say "== config"
  if [[ -e $CONFIG ]]; then say "config exists at $CONFIG; leaving it alone"; return; fi
  say "writing $CONFIG (asks go to '$ME')"
  if ((DRY)); then render_config | sed 's/^/[dry-run]   /'; return; fi
  mkdir -p "$(dirname "$CONFIG")"
  render_config >"$CONFIG"
}

hx_bin() { printf '%s/hx\n' "$(uv tool dir --bin 2>/dev/null || printf '%s' "$HOME/.local/bin")"; }

tool_dirs() { # hx's directory, then those of trk, herdr, claude and claude-worker as found now
  local dirs=() d c seen=: found=() l
  while IFS= read -r l; do found+=("$l"); done < <(command -v trk herdr claude claude-worker 2>/dev/null)   # one path per line
  for c in "$1" ${found[@]+"${found[@]}"}; do
    d=$(dirname "$c")
    [[ $seen == *":$d:"* ]] && continue
    if [[ $d =~ [[:space:]\"\'\\%\$:] ]]; then
      warn "$d has characters a unit PATH cannot hold; the service will not find $(basename "$c")" >&2; continue
    fi
    seen+="$d:"; dirs+=("$d")
  done
  local IFS=:
  printf '%s' "${dirs[*]}"
}

render_unit() {
  local hx=$1
  [[ $hx =~ [[:space:]\"\'\\%\$] ]] && die "the hx path '$hx' has characters systemd would need quoted; install uv tools somewhere simpler"
  local tpl
  tpl=$(cat "$REPO/systemd/$UNIT.in"; printf x)
  tpl=${tpl%x}
  [[ $CONFIG =~ [[:space:]\"\'\\%\$] ]] && die "the config path '$CONFIG' has characters systemd would need quoted"
  tpl=${tpl//@BINDIR@/$(tool_dirs "$hx")}
  tpl=${tpl//@CONFIG@/$CONFIG}
  printf '%s' "${tpl//@HX@/$hx}"
}

launchd() { # HX_SERVICE_MANAGER=launchd|systemd overrides the guess (tests use it)
  case ${HX_SERVICE_MANAGER:-} in launchd) return 0 ;; systemd) return 1 ;; esac
  ! command -v systemctl >/dev/null && [[ $(uname -s) == Darwin ]]
}

install_agent() { # macOS: the watcher as a launchd agent that starts at login and restarts if it stops
  local hx=$1 tpl log=$STATE_HOME/hx/watch.log
  if ((DRY)); then
    say "[dry-run] write $AGENT ($hx watch) and launchctl bootout and bootstrap gui/$(id -u)/com.hx.watch"
    return
  fi
  [[ $hx$CONFIG$HOME =~ [\&\<\>] ]] && die "a path for the launchd agent has & < or >; install somewhere simpler or use --no-service"
  tpl=$(cat "$REPO/launchd/hx.plist.in"; printf x)
  tpl=${tpl%x}
  tpl=${tpl//@BINDIR@/$(tool_dirs "$hx")}
  tpl=${tpl//@HOME@/$HOME}
  tpl=${tpl//@CONFIG@/$CONFIG}
  tpl=${tpl//@LOG@/$log}
  mkdir -p "$(dirname "$AGENT")" "$(dirname "$log")" "$MARKS"
  printf '%s' "${tpl//@HX@/$hx}" >"$AGENT.new" && mv "$AGENT.new" "$AGENT"
  touch "$MARKS/service"
  launchctl bootout "gui/$(id -u)/com.hx.watch" 2>/dev/null || true   # one start: also picks up a new hx
  launchctl bootstrap "gui/$(id -u)" "$AGENT"
  say "service started: launchctl print gui/$(id -u)/com.hx.watch   (log: $log)"
}

install_service() {
  say "== watcher service"
  local hx
  hx=$(hx_bin)
  if launchd; then install_agent "$hx"; return; fi
  if ((DRY)); then
    say "[dry-run] write $UNIT_DIR/$UNIT (ExecStart=$hx watch)"
    say "[dry-run] systemctl --user daemon-reload, enable and restart hx"
    return
  fi
  if ! command -v systemctl >/dev/null; then
    warn "no systemctl: run \`hx watch\` yourself (in a tmux or herdr pane), or rerun with --no-service"
    return
  fi
  mkdir -p "$UNIT_DIR"
  render_unit "$hx" >"$UNIT_DIR/.$UNIT.new" || { rm -f "$UNIT_DIR/.$UNIT.new"; exit 1; }
  mv "$UNIT_DIR/.$UNIT.new" "$UNIT_DIR/$UNIT"
  mkdir -p "$MARKS" && touch "$MARKS/service"
  systemctl --user daemon-reload
  systemctl --user enable hx
  systemctl --user restart hx     # one start: also picks up a new hx if it was running
  say "service enabled and started: systemctl --user status hx"
}

uninstall_service() {
  say "== watcher service"
  if [[ -e $AGENT ]]; then
    if [[ ! -e $MARKS/service ]]; then say "$AGENT was not written by this script; leaving it alone"; return; fi
    if ((DRY)); then say "[dry-run] unload hx, delete $AGENT"; return; fi
    launchctl bootout "gui/$(id -u)/com.hx.watch" 2>/dev/null || true
    rm -f "$AGENT" "$MARKS/service"
    say "launchd agent removed"
    return
  fi
  if [[ ! -e $UNIT_DIR/$UNIT ]]; then say "no unit installed"; return; fi
  if [[ ! -e $MARKS/service ]]; then say "$UNIT_DIR/$UNIT was not written by this script; leaving it alone"; return; fi
  if ((DRY)); then say "[dry-run] stop and disable hx, delete $UNIT_DIR/$UNIT"; return; fi
  command -v systemctl >/dev/null && systemctl --user disable --now hx 2>/dev/null || true
  rm -f "$UNIT_DIR/$UNIT" "$MARKS/service"
  command -v systemctl >/dev/null && systemctl --user daemon-reload 2>/dev/null || true
  say "unit removed"
}

install_plugin() { # herdr takes an Agents-panel view only from an enabled plugin (src/hx/sidebar.py)
  say "== herdr plugin"
  if herdr plugin list --json 2>/dev/null | grep -q '"plugin_id": *"hx"'; then
    say "the hx herdr plugin is already linked"
  elif ((DRY)); then
    say "[dry-run] herdr plugin link $REPO/herdr-plugin"
  elif herdr plugin link "$REPO/herdr-plugin" >/dev/null; then
    say "linked the hx herdr plugin: the watcher can now show the needs-you list in the Agents panel"
  else
    warn "could not link $REPO/herdr-plugin; the Agents panel stays as it is. Retry: herdr plugin link $REPO/herdr-plugin"
  fi
}

install_sidebar() { # herdr's Agents panel becomes the needs-you list (hx sidebar setup)
  say "== herdr sidebar"
  local hx
  hx=$(hx_bin)
  if [[ ! -x $hx ]]; then
    if ((DRY)); then say "[dry-run] $hx sidebar setup, then herdr server reload-config"
    else warn "$hx is not there, so herdr's config.toml is unchanged. Run it later: hx sidebar setup"; fi
    return
  fi
  if ((DRY)); then
    "$hx" sidebar setup --dry-run 2>&1 | sed 's/^/[dry-run]   /' || true
    say "[dry-run] herdr server reload-config"
    return
  fi
  if ! "$hx" sidebar setup; then
    warn "hx sidebar setup failed; herdr's Agents panel stays as it is. Retry: hx sidebar setup"
    return
  fi
  if herdr server reload-config >/dev/null 2>&1; then say "herdr reloaded its config"
  else warn "herdr did not reload its config; run it yourself: herdr server reload-config"; fi
}

next_steps() {
  cat <<EOF

== next steps
1. Make sure \`hx\` is on your PATH: run \`hx --help\`. If it is not found, run \`uv tool update-shell\`.
2. Check the config at $CONFIG: the [humans.*] names must be trk actors.
3. Restart Claude Code so it loads the hx-work skill.
4. See what the watcher would do: \`hx reconcile --dry-run\`.
5. Start a solo session on a self-contained trk work item: \`hx start <tr-id> --dry-run\`, then
   without --dry-run. See README.md.
6. herdr's Agents panel now shows the needs-you list. To put it back, restore the
   \`config.toml.bak-hx-*\` backup beside herdr's config.toml and run \`herdr server reload-config\`;
   to skip the step entirely, install with --no-sidebar.
EOF
  local n=7
  ((LAUNCH_GUARD)) && { say "$n. Tune the launch guard's limits with the DISPATCH_* variables in the header of scripts/$GUARD."; n=$((n + 1)); }
  ((CLAUDE_WORKER)) && { say "$n. Solo sessions now run under claude-worker; CLAUDE_CODE_AUTO_COMPACT_WINDOW changes its 250k window."; n=$((n + 1)); }
  ((MEM_GATE || FRESHNESS)) && say "$n. Restart Claude Code sessions so they load the new hooks."
  return 0
}

if [[ $MODE == bundle ]]; then make_bundle; exit 0; fi

if ((UNINSTALL)); then
  say "Uninstalling hx. Config ($CONFIG) and state ($STATE_HOME/hx) stay."
  uninstall_service
  uninstall_skills
  uninstall_bins
  uninstall_hooks
  say "== hx command"
  if [[ ! -e $MARKER ]]; then
    say "hx was not installed by this script; leaving the command alone (remove it with: uv tool uninstall hx)"
  elif ! command -v uv >/dev/null; then
    die "uv is not on PATH, so hx cannot be removed; the rest is done. Run --uninstall again once uv is back"
  elif tool_installed; then
    act uv tool uninstall hx
    act rm -f "$MARKER"
  else
    say "hx is no longer installed with uv"
    act rm -f "$MARKER"
  fi
  exit 0
fi

check_prereqs
check_paths
check_folders
check_actor
check_skills
check_bins
skip_other_hooks
check_settings
say "== folders"
move_folder "$STATE_HOME/$OLD-install" "$MARKS"   # install marks only: no watcher writes them
install_tool          # before anything else changes: if it fails, the old tool and watcher still run
replace_old_service   # before config and state move, so the old watcher cannot write into them
move_folder "$CONFIG_HOME/$OLD" "$CONFIG_HOME/hx"
move_folder "$STATE_HOME/$OLD" "$STATE_HOME/hx"
install_skills
install_bins
install_hooks
install_config
install_plugin
((SIDEBAR)) && install_sidebar
((SERVICE)) && install_service
((DRY)) || next_steps
