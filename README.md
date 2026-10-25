# hx

hx connects [trk](#requirements), a command-line tracker for work done by AI coding agents, with
[herdr](#requirements), a terminal multiplexer for agents. trk holds the work and the questions.
herdr holds the Claude Code sessions. hx is a small background watcher in between:

- **Questions reach the right place.** When an agent opens a trk ask addressed to a person, hx
  writes it to that person's console inbox and, if they use it, the herdr needs-you queue.
  An ask addressed to another agent wakes that agent's pane; if the agent has no live pane, the
  ask goes to the person who owns the work instead.
- **Answers wake the asker.** When an ask is answered, withdrawn or commented on, hx types a
  one-line note into the asking session's pane, so the session continues without anyone polling.
  A busy pane is never interrupted: the note waits until the pane is idle. hx also never types
  over you: while there is text in the pane's input box, and for 30 seconds after it is empty,
  the note waits. If hx cannot see the input box (a dialog or menu is open, or the pane is too
  short to show it), the note waits too, and after 10 minutes a `HELD` line in the console inbox
  says so. One gap is left: hx reads the box and then types, in two herdr calls, so a key pressed
  in the instant between them can still join the note. Closing it needs herdr to type only into an
  empty box.
  Once the note of an answer is sent, hx also clears that pane's item in herdr-jump's needs-you
  queue (when herdr-jump is installed), if the item names the answered ask; an item about
  anything else stays.
- **Page comments reach the worker.** A comment on an item's page outside an ask box wakes the
  session that claims the item (or its nearest claimed ancestor, or the root's Space). When no
  pane can hear it, and for comments on a Needs-you page, hx writes it to the console inbox.
- **One herdr Space per piece of work.** `hx start <tr-id>` opens a Space for a trk work item and
  starts a Claude session in it that takes the item through trk's stage gates. hx labels the
  Space in the herdr sidebar and closes it 10 minutes after the item closes.
- **No polling and no tokens.** The watcher wakes on trk's change feed and herdr's state. Routine
  operation runs no model.

## Contents

- [Requirements](#requirements)
- [Install](#install)
- [Quickstart](#quickstart)
- [Configuration](#configuration)
- [Commands](#commands)
- [Files hx reads and writes](#files-hx-reads-and-writes)
- [Upgrading](#upgrading)
- [Uninstall](#uninstall)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Repo layout](#repo-layout)

## Requirements

| Needs | Why | Check |
|---|---|---|
| Linux with systemd (user services), or macOS (launchd) | runs the watcher | `systemctl --user status`; on macOS `launchctl print gui/$(id -u)/com.hx.watch` |
| Python 3.12 or newer on PATH (`python3`, or `python3.12`–`python3.14`: macOS's own python3 is 3.9) | the installer runs Python | `python3 --version` |
| [uv](https://docs.astral.sh/uv/) | installs the `hx` command | `uv --version` |
| trk | the work, the asks and the change feed. Install it first with its own `./install.sh` | `trk --version` |
| herdr | the Spaces and panes | `herdr --version` |
| Claude Code | the sessions `hx start` launches | `claude --version` |

Optional, used when present:

- **claude-worker**: a wrapper around `claude` that makes a worker session auto-compact at 250k
  tokens. hx ships one (`scripts/claude-worker`, installed with `--claude-worker`). If it is on
  PATH, solo sessions run under it; otherwise they run under `claude`.
- **eng-toolkit** (Claude Code plugin): for a work item with a JIRA key, the hx-work skill
  hands each stage gate to its kickoff, implement and verification skills. Without it, the skill
  runs the gates itself.
- **launch-guard.sh**: holds a launch when the machine is busy. hx ships one
  (`scripts/launch-guard.sh`, installed with `--launch-guard`); any `launch-guard.sh` on PATH is
  used. Without one, every `hx start` goes ahead.
- **ticket-deps.sh**: fills a new worktree's `node_modules` in the background.
- **The herdr needs-you queue** (`~/.herdr-queue`): the installer adds the `queue` channel when
  that directory exists.

## Install

### Get the code onto the machine

This repo may have no hosted home. Copy it from a machine that has it:

```bash
# on a machine with the repo: one file, with the whole history
./install.sh make-bundle                    # writes ./hx.bundle
scp hx.bundle other-box:

# on the new machine
git clone hx.bundle ~/src/hx
```

Or clone it straight from a machine you can reach with ssh:
`git clone you@host:projects/hx ~/src/hx`.

### Run the installer

```bash
cd ~/src/hx
./install.sh --dry-run        # see what it will do
./install.sh                  # do it
```

The installer:

1. Checks for Python 3.12+, uv, trk and herdr, and stops if one is missing. It reports which of the
   optional tools it found.
2. Installs the `hx` command with `uv tool install --editable`, so a `git pull` in the checkout
   upgrades hx.
3. Links the `hx-work` skill into `~/.claude/skills/`. It refuses to replace a different skill of
   that name unless you pass `--force`; the old one is then kept as `hx-work.bak-<time>`.
4. Writes `~/.config/hx/config.toml` if there is none. Asks go to your user name; use
   `--me NAME` to pick another trk actor (trk actor names are lowercase letters, digits and `-`). If trk is an editable install, the config points hx at
   that checkout's contract.
5. Points herdr's `config.toml` at the needs-you list (`hx sidebar setup`: the Agents rows,
   `sidebar_width = 34` and the tab bar) and reloads herdr. It backs the file up first and changes
   only those three keys; `--no-sidebar` leaves herdr alone.
6. Installs and starts the watcher as the systemd user service `hx`. On macOS, which has no systemd,
   it is the launchd agent `~/Library/LaunchAgents/com.hx.watch.plist`, logging to
   `~/.local/state/hx/watch.log`.

Four extras are off unless you ask for them. They are for a box that runs many sessions at once,
and need Linux (the guard reads `/proc` and uses `flock`) and `jq` (the hooks):

- `--launch-guard` links `scripts/launch-guard.sh` into `~/.local/bin`. `hx start` then waits while
  a machine-wide hold is on (`hx hold`), the 1-minute load is above 2 × CPUs, less than 16 GB of
  memory is free, swap or the root disk is 95 % full, or the last launch was under 30 seconds ago.
  Each limit is a `DISPATCH_*` variable in the script's header. Other launchers can call it too:
  `launch-guard.sh <label>` exits 0 (go) or 3 (held, with the reason on stderr). It refuses to
  replace a different `launch-guard.sh` unless you pass `--force`; the old one is kept as
  `launch-guard.sh.bak-<time>`.
- `--claude-worker` links `scripts/claude-worker` into `~/.local/bin`, and solo sessions run under
  it. It starts `claude` with `CLAUDE_CODE_AUTO_COMPACT_WINDOW` at 250000 (set the variable to
  change it), passed as `--settings` so a larger window in your `settings.json` does not override
  it. A worker that compacts early re-sends far less context on each turn. Use it for any worker
  you start yourself too; keep coordinators on plain `claude`. While a launch guard is installed,
  it also refuses to start during a machine-wide hold.
- `--mem-gate` adds `claude/hooks/mem-gate.sh` to `~/.claude/settings.json` as a PreToolUse hook on
  Bash. While less than 16 GB of memory is free (`MEM_GATE_MB`) or 20 test processes run
  (`MEM_GATE_MAX_PROCS`), it turns away heavy commands (jest, vitest, tsc, playwright, npm
  test/build; `MEM_GATE_PATTERN` replaces the list) and tells the session to wait and retry.
  `MEM_GATE_OFF=1` turns it off.
- `--freshness-hook` adds `claude/hooks/checkout-freshness.sh` as a SessionStart hook. A session
  whose checkout is behind its base branch (`BASE_BRANCH`, else the remote's default branch) is told
  how far behind, before it builds or tests anything.

The hooks run from this checkout, even with `--no-editable`. The installer backs up
`settings.json` to `settings.json.hx-bak-<time>` before changing it and prints the diff; it never
touches other hooks. `claude/hooks/hold-notice.sh` is not installed: add it to your Claude settings
yourself to tell sessions that start during a hold.

| Option | Effect |
|---|---|
| `--dry-run` | print what would happen and change nothing |
| `--me NAME` | the trk actor who hears asks, for a new config |
| `--no-editable` | install copies of hx and the skill, not links to the checkout |
| `--no-service` | skip the systemd service; run `hx watch` yourself |
| `--no-sidebar` | leave herdr's `config.toml` alone: no needs-you list in the Agents panel |
| `--force` | reinstall the command and replace a differing skill, launch guard or claude-worker |
| `--launch-guard` | put `launch-guard.sh` on PATH, so `hx start` waits while the box is busy |
| `--claude-worker` | put `claude-worker` on PATH, so solo sessions auto-compact at 250k tokens |
| `--mem-gate` | add the memory-gate hook for heavy test and build commands |
| `--freshness-hook` | add the hook that tells a session its checkout is stale |
| `--uninstall` | remove what the installer added (see [Uninstall](#uninstall)) |

Rerunning with the other of editable and `--no-editable` swaps the skill's link and copy; add
`--force` to swap the command too.

The watcher runs while you are logged in. To keep it running after you log out, run
`loginctl enable-linger $USER` once.

When it finishes, restart Claude Code so it loads the skill, and check `hx --help`. If `hx` is not
found, run `uv tool update-shell` and open a new shell.

## Quickstart

1. Check that the watcher is up and sees your trk store:

   ```bash
   systemctl --user status hx    # macOS: launchctl print gui/$(id -u)/com.hx.watch
   hx reconcile --dry-run        # prints the actions one pass would take, and takes none
   ```

2. Make a self-contained trk work item that names a repo checkout. trk refuses one without its
   current and desired behaviour and at least one acceptance criterion:

   ```bash
   echo "The login test fails about one run in ten." > current-behaviour.md
   echo "The login test passes every run." > desired-behaviour.md
   trk new work-item "Fix the flaky login test" --form self-contained --repo ~/src/myapp \
     --section current-behaviour=@current-behaviour.md \
     --section desired-behaviour=@desired-behaviour.md \
     --criterion "The login test passes 50 runs in a row" --json
   ```

   (See `trk new --help` for everything a work item can carry.)

3. Start it:

   ```bash
   hx start tr-abc12 --dry-run   # shows the Space, the command and whether the guard allows it
   hx start tr-abc12             # opens the Space and starts the session
   ```

   hx opens a herdr Space named after the item, starts Claude in a new worktree of the repo with
   `/hx-work tr-abc12`, and records the binding. The session claims the item and works it through
   `framed → specd → built → merged → verified`.

4. When the session needs you, it opens a trk ask. You see it in the console inbox
   (`~/.local/state/hx/inbox.log`; `tail -f` it in a herdr pane) and in the needs-you
   queue if you use it. Answer it:

   ```bash
   trk answer tr-ask12 --outcome "Use the retry, not the timeout" --answer -
   ```

   hx wakes the session with the answer. If trk publishes item pages (`trk pages`), a comment on
   the ask's page also wakes it, and the session confirms the answer with `trk answer --from-comment`.

5. When the session closes the item with its final `trk advance`, hx closes the Space 10 minutes
   later, once the pane is idle.

## Configuration

`~/.config/hx/config.toml` (`$XDG_CONFIG_HOME/hx/config.toml` when that is set, or the file named by `HX_CONFIG`). Every key is optional.

```toml
# Who hears asks addressed to people, by trk actor name, and how. Asks go to a role
# (engineering, product); trk resolves the role to the actor who holds it: see `trk roles`.
# Channels: "console" (the inbox log) and "queue" (the herdr needs-you queue).
[humans.alice]
channels = ["console", "queue"]

[humans.bob]
channels = ["console"]

# The model each role runs (default claude-sonnet-5-5). An opus model needs `hx start --reason`.
[models]
solo = "claude-sonnet-5-5"

# The shell command that starts each role. {model}, {effort}, {worktree} and {root} are filled in.
[commands]
solo = "claude --model '{model}' --effort {effort} -w {worktree} '/hx-work {root}'"

# Seconds a Space stays open after its root closes and its pane is idle.
[spaces]
close_delay_s = 600

# What hx does with a worktree once the session that recorded it (`trk worktree set`) leaves
# herdr: "act" (the default) finishes it, "report" only writes what it would do, "off" does nothing.
[worktrees]
finish = "act"

# trk's JSON contract, which hx checks every reply against. Default: the copy shipped in hx.
[trk]
contract = "~/src/trk/contract"
```

With no config file, asks go to your user name on the console channel only.

**Environment variables**

| Variable | Default | Meaning |
|---|---|---|
| `HX_CONFIG` | `~/.config/hx/config.toml` | the config file |
| `HX_STATE_DIR` | `~/.local/state/hx` | state, logs and the inbox |
| `HX_TRK_CONTRACT` | the shipped copy | trk contract directory; `[trk] contract` wins over it |
| `TRK_HOME` | `~/.local/share/trk` | the trk store hx reads (as trk uses it) |
| `HERDR_SESSION` | `default` | the herdr session, and the queue directory `~/.herdr-queue/<session>` |

## Commands

| Command | Does |
|---|---|
| `hx watch` | the watcher loop; the systemd service runs it. `--interval` (1 s) and `--tick` (30 s) set how often it checks the feed and how often it runs a pass anyway |
| `hx reconcile [--dry-run]` | one pass now. With `--dry-run`, print the actions and take none |
| `hx start <tr-id> [--dry-run] [--model M] [--effort E] [--reason TEXT]` | open a Space for a self-contained work item and start its solo session. `--effort` is one of low, medium (default), high, xhigh, max |
| `hx hold --reason TEXT [--until 2h\|ISO-time] [--by WHO] [--if-free \| --replace ID]` | hold the whole machine: launchers (`hx start` among them) refuse to start sessions, and each live agent is told once. For a script that holds on its own: `--if-free` holds only when no hold is live (an expired one counts as free, an unreadable file does not), `--replace ID` only when the file holds that id; either exits 4 and changes nothing otherwise. Each check and write is one step under hx's lock: never hold `hold.json.lock` yourself around separate hx calls |
| `hx resume [--id ID]` | end the hold; the agents that heard it are told once. `--id` ends it only if it is that hold (exit 4 otherwise), so a script ends only its own |
| `hx send --pane P [--pane P2 ...] [--wait 45] TEXT` | type one line into panes by the watcher's rule: each pane gets it once its input box has been empty for 30 s; a draft, a box hx cannot see, or no quiet window within `--wait` is refused. Exit 1 if any pane was refused |
| `hx held [--json]` | exit 0 if held, 1 if not, 2 if the hold file cannot be read (which counts as held) |
| `hx sidebar setup [--dry-run] [--config PATH]` | point herdr's config.toml at the needs-you sidebar (below): the Agents rows, `sidebar_width = 34` and the tab bar. It backs the file up first and changes only those three keys; `--dry-run` prints the change. `install.sh` runs it, so this is for a rerun or a second config |
| `hx needs-you --line` | the tab bar's text: `⚑ 3 · tr-z58a0 38` (count, then the top-scored ask), nothing when no one waits, `hx stale` when the watcher has not written for 2 minutes |
| `hx needs-you --json` | the queue as cards, for a page that answers by tap: one per live pane's item, with its space, session, cache time left, the open trk ask it names (`trk_ask`), its `sha`, and the Needs-you section parsed into asks, options and `reply_with` (each with `tap`: false for a `!` or `/` reply, a `<placeholder>`, prose around the replies, or a reply two asks share). A section off the format gives `asks: null` and only `raw`. hx's own `hx_*` items are left out: trk shows those asks. Each card has a `kind`: `ask`, or `blocked` for a pane held on a dialog with no card of its own, which comes first and has no `asks` and no `sha`, so `hx answer` never types into it |
| `hx answer <pane> --sha S --session X [--wait 45] "<reply>"` | type a reply into the ask a card showed, by `hx send`'s rule. It refuses a reply that starts with `!` or `/` or holds a control character, an ask it answered already (the same pane, text and time asked; the same words asked again later are a new ask), an item that is gone or whose text changed (its sha), and a pane that runs another session (`--session ''` for a pane with none); it checks the item and session again right before typing. Exit 0 sent, 1 refused, 2 bad --wait |

**The hold file** is `~/.local/state/hx/hold.json`:
`{"id", "reason", "by", "since", "until"}`, times in UTC like `2027-01-15T08:00:00Z`, `until`
null for "until `hx resume`". Any launcher can read it without hx: the machine is held while the
file exists and `until` is null or later than now. A file that cannot be read counts as held, and
so does one missing a key, whose `until` names no zone (`Z` or `+hh:mm`), that is not a regular
file, or that is over 64 KiB. Open it without blocking (`O_NONBLOCK`) and check it is a regular file
on that same descriptor, so a FIFO put in its place never makes a reader wait.
`hx hold` replaces the file in one step, so a reader never sees half of it. The watcher tells each
live Claude session once (through the same draft guard as every wake), tells every session it saw
during the hold once when the hold ends (a session away then hears it if back within an hour), removes a file whose `until` has passed, and writes HOLD and RESUMED lines to the
inbox. A session that starts during a hold hears it from `claude/hooks/hold-notice.sh`, a Claude Code
SessionStart hook that reads the file by the same rules; add it to your Claude settings to use it.

**The needs-you sidebar.** herdr's Agents panel lists only the panes with a needs-you item, most
urgent first, under the heading "needs you". Each row shows the trk ask, the pane's prompt-cache
time left (`52m`; `9m!` in bold under 10 minutes; `cold`; `busy` while it works) and a score, with
the Space on the line below. The score is trk's urgency base for the ask (`urgency.base` in
`trk waiting --json`: the items it holds up, ×10 when urgent) times `1 + 9 × (share of the cache
used)²` while the cache is warm. An item with no trk ask, or before trk gives urgency, is unscored
and sorts last; ties go to the cache that ends soonest. A pane held on a question or permission
dialog (herdr's `blocked`) with no item gets a `⚑ dialog  open` row above every ask: a dialog stops
the turn before its Stop hook, so herdr-jump makes no item for it, and the session waits until you
answer it in the pane. The row goes when the dialog closes; a pane whose turn-mode driver is a live
agent is left to that driver. The watcher sends the rows every pass as
pane tokens that live 2 minutes, so if it stops the panel empties and the tab bar says `hx stale`.
Between passes it looks at the queue's items each second: when one comes or goes (herdr-jump's
prompt hook deletes a pane's item once you submit a prompt there), it redraws the panel at once.
herdr accepts the panel's view only from an enabled plugin: `install.sh` links `herdr-plugin/`,
which runs nothing, and then runs `hx sidebar setup` for the keys herdr's config needs (skip both
with `--no-sidebar`). Undo: `herdr plugin unlink hx`, put back the `config.toml.bak-hx-*` backup and
run `herdr server reload-config`.

**Finishing worktrees.** When a session leaves herdr (its pane closes, runs another session, or
shows none for 2 minutes) and stays gone 5 minutes, hx looks at the linked worktrees of the repos
that session worked in. For each tree whose newest trk record (`trk where`) names that session, it:
archives `tmp/` and every ignored file that cannot be rebuilt (`.env`, local data; not `.venv`,
`node_modules`, or top-level build output) to the item's `effective_archive`, default
`~/work/archive/<tree>-tmp.tgz`, never over an existing file; runs `git worktree remove` without
`--force`, so git refuses anything written since the checks; deletes the local branch only when the
tree had it checked out, all its commits are on a remote and its tip is a merged PR's head (`gh pr
list --head`); and runs `trk worktree clear`. It keeps the tree, and writes `KEPT <tree> <item>:
<why>` to the inbox, when the tree has uncommitted changes, untracked files outside `tmp/`, commits on
no remote, a live pane in it, a lock, another repo, an archive over 500 MB, or no trk record, or when
the session's pane now runs another session. Each finish writes `FINISHED <tree> <item>: …`. hx
finishes one tree per pass and never deletes an archive. Set `[worktrees] finish = "report"` to see
the plan without the changes; a tree once reported or kept is not looked at again.

`hx start` refuses an item that is not a self-contained work item, is closed, has open children, or
names no repo checkout, and says why. Items with child work (orchestrator Spaces) are not
supported yet.

## Files hx reads and writes

| Path | What |
|---|---|
| `~/.local/state/hx/state.json` | the watcher's memory: feed cursor, bindings, delivered asks |
| `~/.local/state/hx/inbox.log` | the console channel: asks, closes, releases, page comments, held wakes, alerts |
| `~/.local/state/hx/hold.json` | the machine-wide hold, while there is one (`hx hold`) |
| `~/.local/state/hx/watch.log` | errors from the watcher |
| `~/.local/state/hx/sidebar.json` | the tab bar's needs-you count, written each pass |
| `~/.claude/ctx/cache/<pane>.json` | read only: cache-timer's prompt-cache times (`CACHE_STATE_DIR` overrides) |
| `~/.local/state/hx/sessions.jsonl` | which Claude session worked which item, in which role |
| `~/.local/state/hx/deps/` | logs of background dependency installs |
| `~/.herdr-queue/<session>/hx_<ask>.json` | needs-you queue items (queue channel only) |
| `$TRK_HOME/feed.head` | read only: trk touches it on each change, which wakes the watcher |

hx never writes to the trk store except to release the claims of a session that has left herdr.

## Upgrading

With the default editable install:

```bash
cd ~/src/hx && git pull     # or: git pull /path/to/new.bundle
systemctl --user restart hx     # macOS: launchctl kickstart -k gui/$(id -u)/com.hx.watch
```

With `--no-editable`, run `./install.sh --no-editable --force --no-service` after the pull, then restart the
service.

**From herdr-extension** (hx's old name): pull, then run `./install.sh` once. It moves
`~/.config/herdr-extension`, `~/.local/state/herdr-extension` and
`~/.local/state/herdr-extension-install` to their `hx` names, stops and removes
`herdr-extension.service` in favor of `hx.service`, and replaces the `herdr-extension` uv tool
with `hx`. A folder that already exists under the new name wins, and the old one is left alone.
`hx` itself also moves an old config or state folder the first time it runs.

**After upgrading trk**, refresh hx's copy of trk's contract and run the tests:
`scripts/sync-contract.sh /path/to/trk-checkout`. If the config's `[trk] contract` points at the
trk checkout, hx already uses the current contract.

## Uninstall

```bash
./install.sh --uninstall
```

This stops and removes the service, removes the skill, the launch guard and claude-worker if you
have not edited them, takes hx's own hooks out of `~/.claude/settings.json`, and removes the `hx` command if the
installer added it. It never touches the config or the state directory; delete
`~/.config/hx` and `~/.local/state/hx` yourself if you want them gone.

## Troubleshooting

| Symptom | Look at |
|---|---|
| Asks do not show up | `systemctl --user status hx`, then `watch.log`. Check that the `[humans.*]` name is the trk actor the ask is addressed to (`trk waiting --all --json`) |
| `ALERT hx watcher stopped acting` in the inbox | trk's replies no longer match the contract hx checks. Upgrade hx, or run `scripts/sync-contract.sh`, then restart the service |
| `hx start` says `held: …` | launch-guard.sh refused the launch; its message says why. To launch anyway, run it with `HERDR_HOLD_OVERRIDE=1` (a hold) or `DISPATCH_FORCE=1` (load, memory, swap, disk): hx passes the override to the new pane, so claude-worker's own guard lets it through too, and claude-worker clears it for the session |
| `hx start` says the item names no repo checkout | `trk edit <id> --add-repo <path>`: the path must be a git checkout |
| A session is not woken after an answer | the pane is busy (the note waits for idle), someone has text in its input box (the note waits until the box has been empty for 30 seconds), or the session left herdr. `hx reconcile --dry-run` shows the wakes still owed |
| `hx: command not found` | `uv tool update-shell`, then a new shell |

`journalctl --user -u hx` shows the service's own output.

## Development

```bash
uv sync                 # a .venv with hx and pytest
uv run pytest -q        # the suite; it uses throwaway trk stores and fake herdr, never your live ones
```

The tests need `trk` on PATH. The design, its amendments and the implementation plans are in
`docs/superpowers/`; `docs/checks/` records the live checks run against a real herdr.

## Repo layout

| Path | What |
|---|---|
| `src/hx/` | the package: `cli` (commands), `reconcile` (one pass), `routing`, `replies`, `spaces` (pure decisions), `feed`, `tracker`, `herdr`, `channels`, `state`, `start` |
| `src/hx/contract/` | the copy of trk's JSON contract that hx checks replies against |
| `claude/skills/hx-work/` | the skill a solo session runs |
| `systemd/`, `launchd/` | the service templates the installer fills in (Linux, macOS) |
| `install.sh` | install, uninstall, make-bundle |
| `scripts/sync-contract.sh` | refresh the contract copy from a trk checkout |
| `scripts/launch-guard.sh` | says whether the box can take another session (`--launch-guard`) |
| `scripts/claude-worker` | starts a worker session that auto-compacts at 250k tokens (`--claude-worker`) |
| `scripts/merge_settings.py` | adds and removes hx's hooks in `settings.json` |
| `claude/hooks/` | Claude Code hooks: `hold-notice.sh`, `mem-gate.sh`, `checkout-freshness.sh` |
| `tests/` | the suite |
| `docs/` | design, plans and live-check records |
