# herdr-extension: design

Status: APPROVED by Michael, 2026-10-11 (close delay 10 min, restart cap 3, console + queue only). Sections 1–3 were approved one at a time in the herdr-extension session; this document writes them down together.

Amended 2026-10-12 by Michael: the four cost changes from the sme-herdr-env review (lease renewal by the watcher, solo sessions, model per role, the session map), and hx sessions skip ask-notify. See "Amendments of 2026-10-12".

## Goal

One herdr Space per thing Michael is working on. Each Space has an orchestrator: a Claude session that owns the Space's workers, makes decisions, and drives the work to completion. Workers are tabs or panes inside that Space. When an agent or the orchestrator needs a person, it asks through the tracker, and the plugin reaches Michael through channels he defines. One of those channels just writes to the console.

This replaces herdr-projects. It uses no herdr-projects code and no herdr fork.

**Success looks like:**
- the Spaces list shows only live work;
- every Space runs itself through its orchestrator;
- questions reach the right agent or person without anyone polling;
- routine operation costs no model tokens.

## Decisions this rests on

| Decision | Who and when |
|---|---|
| New plugin written from scratch, replacing herdr-projects | Michael, 2026-10-10 |
| The tracker (`trk`, ~/projects/beads-alternative) is the source of truth for items. The plugin is the runtime. | Michael, 2026-10-10 |
| The tracker contract: CLI JSON plus a pull-only change feed | [ADR 0002](~/projects/beads-alternative/docs/adr/0002-herdr-contract.md), agreed with the tracker session |
| A Space opens only when work starts and closes when its item closes | Michael, 2026-10-11 |
| An always-running Python watcher, not herdr triggers, not orchestrators watching the tracker | Michael, 2026-10-11 |
| Its own repo (this one), local until Michael says otherwise; not in mjfork/workbench | Michael, 2026-10-11 |
| The watcher keeps a live orchestrator's lease; a childless root runs one solo session; the model is set per role; hx writes a session map for the cost scoreboard | Michael, 2026-10-12, from the sme-herdr-env review |
| Sessions hx starts skip ask-notify | Michael, 2026-10-12 |
| The watcher runs only while Michael has a login session (no linger) | Michael, 2026-10-12 |

## Architecture

```
            trk (SQLite, single writer)                       herdr server
                 │  feed.head (wake hint)                        ▲   │ events
                 ▼                                               │   ▼
  ┌──────────────────────── hx watch (systemd user service) ────────────────────┐
  │  wake: inotify on feed.head · herdr event pokes · 30 s timer                │
  │  reconcile():                                                               │
  │    read   trk changes --since cursor, trk waiting/claims/show  (JSON)       │
  │    read   herdr session state (workspaces, tabs, panes, agent status)       │
  │    decide what the Spaces, sidebar tokens and deliveries should be          │
  │    act    herdr CLI · trk claim release/sweep · channel senders             │
  └──────────────────────────────────────────────────────────────────────────────┘
        ▲ hx start / hx worker (orchestrator, Michael)      channels: console · queue
```

**Units, each with one job:**

| Unit | Job | Depends on |
|---|---|---|
| `tracker` | Runs `trk … --json`, validates the reply against `contract/*.schema.json`, and returns typed records. Keeps the (epoch, seq) cursor. | `trk` on PATH |
| `herdr` | Thin wrapper over the `herdr` CLI. Reads session state; creates and closes workspaces, tabs and panes; sends text; reports metadata. | `herdr` on PATH |
| `state` | The plugin's own JSON state file, written atomically: cursor, runtime bindings, delivered asks, restart counts. | filesystem |
| `routing` | A pure function from (open asks, claims, bindings, config, now) to the deliveries due. | nothing |
| `spaces` | A pure function from (claims, items, bindings, herdr state, now) to the herdr actions due. | nothing |
| `channels` | Senders for console and queue, behind one interface. | per channel |
| `watch` | The loop: wake, reconcile, act, persist. | all of the above |
| `cli` | `hx watch`, `hx reconcile`, `hx start`, `hx worker`, `hx status`, `hx inbox` | the above |

The two decision units, `routing` and `spaces`, are pure. They take snapshots and return actions, so most of the behaviour is tested without herdr or `trk`.

**Language and packaging:**
- Python 3.12, managed with `uv`, matching `trk`.
- It is installed with `uv tool install -e ~/projects/herdr-extension`, which puts `hx` on PATH.
- Dependencies: `jsonschema>=4.26` (for `referencing`), and nothing heavy beyond it.

**What ships in this repo:**
- `herdr-plugin.toml`, the herdr plugin manifest:
  - actions: start Space, reconcile now, open inbox;
  - events: a pane closed and agent status changed, both of which run `hx reconcile --poke`;
  - one pane: the inbox viewer.
- `systemd/herdr-extension.service`, a user unit that runs `hx watch`.

  herdr does not supervise plugin startup commands, so systemd does: `Restart=on-failure`.
- `claude/`, a small Claude Code plugin:
  - the `hx-orchestrate` and `hx-work` skills;
  - the per-turn lease-renewal hook that ADR 0002 point 6 makes the claimant's job. It runs `trk claim renew --session $SESSION`, costs no tokens, and does nothing when the session holds no claim.

## The reconcile step

Every wake runs the same idempotent `reconcile()`:

1. **Read the feed.**
   - Run `trk changes --since <seq> --json`.
   - Reject the reply if `schema` is not 1: stop acting, and raise a console and queue alert.
   - If the epoch differs from the stored one, or `head` is below the cursor, drop everything built from the feed and rebuild from `--since 0` plus current state (ADR 0002 point 2).
   - On the first poll there is no stored epoch: store it, with no rebuild.
   - Rows with `data.via = "migration"` never cause a wake or a notice.
2. **Read current truth:** `trk claims --json`, `trk waiting --all --json`, `show` for affected roots, and the herdr session state.

   Feed rows only say *what to look at*. Decisions are made from current state, never by replaying rows, so a missed or repeated row cannot cause a wrong action.
3. **Decide.** `spaces()` and `routing()` return actions.
4. **Act, then persist.**
   - Each action is keyed so that repeating it does nothing. Examples: `open-space:<root>`, `deliver:<ask>:<channel-index>`, `wake:<ask>:<event-seq>`.
   - The state file records which keys are done.
   - The cursor advances only after the actions for those rows are recorded.

The watcher wakes:
- on any change to `feed.head` (inotify, debounced by 250 ms, so a migration burst is one pass);
- when the herdr plugin's events run `hx reconcile --poke`, which signals the running watcher;
- every 30 s, for timers: escalation steps, close delays, orchestrator health.

## Section 1: delivering asks (approved)

**Truth.** The open asks are `trk waiting --all --json`. Each delivered ask is recorded in state, so a restart, a restore, or a repeated feed row never sends it twice.

**Opened, readdressed or reopened.** The row's addressee is already resolved by the tracker.

- **The addressee is an agent.** The plugin tries in order (ADR 0002 point 10):
  1. the live claim of that role on the ask's nearest ancestor;
  2. the orchestrator of the root's Space, from the plugin's binding;
  3. the root owner's human channels.
- **Waking an agent:**
  - Find its pane by matching the claim's session uuid to herdr's `agent_session.value`. Never key on the pane id.
  - When `agent_status` is idle, send one line and Enter, for example: `[hx] 2 asks for you: tr-ab12 "Which schema version?" · tr-cd34 "…". Run trk show <id>.`
  - When the agent is working, hold the line until it goes idle.
  - Batch every pending ask for that pane into one message.
  - Before waking, check `cache-state.sh <pane>`. If the agent's cache has expired, the wake still goes out as one message. It is never split into several.
- **The addressee is a human.** The plugin sends the ask to every channel of that person at once, as soon as it opens. There are no timed escalation steps in v1 (Michael, 2026-10-11):

  ```toml
  # ~/.config/herdr-extension/config.toml
  [humans.michael]
  channels = ["console", "queue"]
  ```

**Answered or withdrawn.**
- The plugin wakes the asker's session if it is live.
- Otherwise it wakes the session now claiming the ask's parent, from `data.parent_claim_session` while `parent_claim_live` is true.
- Otherwise it wakes the Space's orchestrator.
- An answer that came from a page comment (`data.via = "page"`) is handled the same way.
- A comment that does not close the ask (`ask.replied`) wakes the asker too: see amendment 8.
- The asker is the session that opened the ask. A reopen by someone else (a person, an owner, a driver) does not change it (2026-10-19). For an ask that closed before hx kept this record, the first reopener hx sees becomes the asker.

**Withdrawn with `reason = "parent_closed"`.** The plugin clears the ask's inbox line and its sidebar ⚑ count, and notifies no one.

**Channels:**

| Channel | What it does |
|---|---|
| `console` | Appends a line to `~/.local/state/herdr-extension/inbox.log`. The inbox pane (plugin action "open inbox") shows it live. The Space's sidebar ⚑ count goes up. |
| `queue` | Adds an item to the needs-you queue (`~/.herdr-queue/`), in its own format. Michael's existing notify tooling propagates it immediately, and it shows as `⚑ N` in the Claude footer. |

Teams posts go to a channel the whole team sees, so Teams is not an escalation channel.

New channels are added by adding a sender. The config only names them.

**Orchestrator escalation.** When an orchestrator needs Michael, it opens a `trk` ask addressed to a human role (product or engineering). The plugin delivers that ask through the channels above. There is no second escalation path.

## Section 2: Spaces, orchestrators and workers (approved)

**What gets a Space.** A root item, meaning one with no parent: a program, or a project or ticket that stands alone. Planned or parked roots have no Space.

**When a Space opens:**
- Michael runs `hx start <root>`, or the plugin action "start Space"; or
- the watcher sees a live claim on the root or anything under it, and the root has no Space yet.

**What opening does:**
1. It creates a herdr workspace with the label `<root id> <short title>` and the cwd set to the root's first repo (or `$HOME` if it has none).
2. Tab 1 is named `orchestrator`. It runs `claude '/hx-orchestrate <root>'`.

   The skill claims the root as actor `orchestrator`, using `--force` for a program root (ADR 0002 and the core spec). It reads the tree and `ready`, then starts workers.
3. The plugin records the binding `root → {workspace id, orchestrator session uuid, opened_at}`. It learns the session uuid from herdr's `agent_session` once the pane reports it.
4. If the orchestrator is ever replaced, its successor runs `trk handoff show <root>` first.

**Workers are started by the orchestrator, not by the watcher.**
- `hx worker <item> [--tab]` places the worker by ADR 0002 point 9's default:
  - an item at depth 1 opens a new tab;
  - an item at depth 2 or deeper opens a pane split in its nearest ancestor's tab;
  - `--tab` gives the item a tab of its own instead.
- The worker runs `claude '/hx-work <item>'`. That skill claims the item as actor `worker` and works it through the tracker's gates.
- Before starting a worker, `hx worker` checks `trk show <item>`. It refuses an item that isn't `ready`, printing its `why_not`. This is the enforcement point ADR 0002 anticipates.
- `hx worker` sets the worker's turn-mode to agentic, with the orchestrator's pane as its driver. The worker's "Needs you" sections then go to the orchestrator, not to Michael.
- A session Michael starts by hand that claims an item is marked in that Space's sidebar. Its pane is never moved.

**When things close:**
- **A worker's item closes.** Its pane or tab closes once that agent is idle. A worker whose item is still open is never closed, whether it is idle, blocked or waiting on an ask.
- **Any agent pane closes, by the plugin or by hand.** herdr's `pane.closed` event carries only the pane id, so the watcher keeps the pane-to-session map from its last snapshot. It runs `trk claim release --as herdr --session S` for every claim session S still holds.
- **The root closes.** The Space is marked closed in the sidebar. It closes 10 minutes after its last agent goes idle. Nothing is ever closed while an agent is working.
- **The root reopens.** That alone opens nothing. A new claim does (see "When a Space opens").

**When the orchestrator dies:**
- An expired lease alone does not mean the orchestrator is dead. A lease lasts 60 minutes by default and renews only when a turn ends, so an orchestrator idle for over an hour while its workers run would lose its lease while still alive.
- So the watcher keeps that lease itself (amended 2026-10-12). While herdr shows the orchestrator's session in its pane, the watcher renews the session's claims at zero tokens. It never wakes the orchestrator just to re-claim: a wake after an hour idle re-sends the whole context at full price. See "Amendments of 2026-10-12" for the contract step this needs.
- If herdr shows the orchestrator's pane is gone, or its session has exited, and the Space and its root are both still open, the watcher restarts the orchestrator in tab 1. The new session reads the handoff first.
- After 3 restarts within one hour, the watcher stops restarting and opens an ask to the root's owner: "orchestrator for <root> keeps dying".

**After herdr restarts:**
- herdr does not keep plugin tokens across a restart.
- The watcher re-reports every token.
- It reopens a Space for every root that is open and still has a live claim under it.
- It recognises existing Spaces through its bindings, so it never opens a duplicate.

**Sidebar.** For each Space, the watcher reports the workspace token `hx` through `herdr workspace report-metadata`, for example `tr-yk91p · build · 3w · ⚑2`:
- the root id;
- the current stage;
- the worker count;
- the open-ask count.

Panes get `hx_sub`, taken from the tracker and costing no tokens: the item id and how many criteria are met, for example `tr-3r0je ac 3/5`. herdr's own working/idle icon sits beside it. These tokens replace herdr-projects' `$hp_sub` in `~/.config/herdr/config.toml`.

## Amendments of 2026-10-12 (approved)

From the sme-herdr-env cost review, approved by Michael on 2026-10-12. Each one replaces the text it names.

**1. The watcher keeps a live orchestrator's lease.**
- The watcher renews a session's claims only while herdr shows that session's agent as `idle`, `done` or `blocked`. It runs `trk claim renew --as herdr --session S` on each pass where the lease is past half. This costs no tokens.
- The watcher never renews a session that herdr shows as `working` or with an unknown status, or one whose pane is gone. A working session renews itself through the per-turn hook. A hung session's lease still lapses.
- This replaces the lapsed-lease nudge. A wake goes to an orchestrator only for real work: an ask, an answer, or a worker that finished.
- Contract: ADR 0002 point 6 said "the plugin never renews a lease". Michael ruled this narrower renewal on 2026-10-12, and tracker ticket tr-2j4z7 amends the ADR. Slice 4 builds this only after tr-2j4z7 merges. The CLI already accepts the call, and the `claim.renewed` row records actor `herdr`.
- Until then, an orchestrator claims its root with a long lease (`--lease 24h`).
- Dead-orchestrator detection does not change. It still goes by the pane and session, never by the lease.

**2. A root with no children runs one solo session.**
- A standalone ticket today runs as one session (dispatch-ticket.sh). Running it as an orchestrator plus a worker doubles that. Standalone ticket sessions are about 55% of weekly spend.
- When a Space opens on a root that has no open children, tab 1 runs `claude '/hx-work <root>'` with role `solo`. That one session claims the root as actor `worker` and works it through the gates. No orchestrator runs.
- If the solo session adds children, it becomes the orchestrator for them. It starts workers with `hx worker` as an orchestrator does, and the binding's role changes to `orchestrator`.
- The restart and close rules for an orchestrator apply to a solo session.

**3. The model is set per role.**
- `~/.config/herdr-extension/config.toml` holds `[models]` with `orchestrator`, `worker` and `solo` keys. All three default to Sonnet (`claude-sonnet-5-5`).
- `hx start` and `hx worker` pass `--model` from the config.
- An Opus session needs a stated reason in the config, for example kickoff framing. This follows roadmap P0.2/P0.10.

**4. The session map for the cost scoreboard.**
- The watcher appends one row to `~/.local/state/herdr-extension/sessions.jsonl` each time a binding starts or changes. It never rewrites earlier rows.
- Each row is `{"at", "session", "root", "item", "role", "ticket", "model", "pane"}`:
  - `at` is ISO-8601 UTC.
  - `session` is the Claude session uuid. It is herdr's `agent_session.value`, which is also the transcript filename.
  - `role` is one of `orchestrator`, `worker` or `solo`.
  - `ticket` is the JIRA key from the item's `jira_url` (the last path segment, for example `PROJ-123`), or null. trk keeps no other external ref.
  - `pane` is informational only.
- A session that moves to another item gets a new row. The scoreboard attributes spend from a row's `at` until the next row for that session. Reading this file is the scoreboard's own change (sme-herdr-env).

**5. Sessions hx starts skip ask-notify.**
- ask-notify (`~/.claude/mods/ask-notify`) tells director when a ticket session waits on a person. An hx session's asks already reach Michael through hx, so a second notice would deliver each ask twice.
- `hx start` and `hx worker` set `HX_ROLE=<role>` in the session's environment.
- ask-notify's `scopeOf` returns null when `HX_ROLE` is set. The ask-notify mod belongs to the mods session (herdr-improvements, changes-made.md), so hx does not edit it. In slice 3 the change goes to the mods session, or to Michael as a logged edit, together with the sessions that carry the variable.

**Noted for later slices, not yet designed:**
- how long-lived orchestrators interact with server-side idle compaction and breakpoint-compact;
- the JIRA ticket ↔ trk item flow, where the eng-toolkit skills sit in the hx-work gates, and `ticket-deps.sh` in worker worktrees;
- a keep-or-revert gate at cutover, comparing $/Done and lead time against the dispatch-ticket baseline.

## Amendment of 2026-10-16 (approved)

From sme-token-efficiency's model routing rule (routing-1). Michael approved it on 2026-10-16. It extends amendment 4.

**6. The session map records who started a session, and how.**
- Each `sessions.jsonl` row also has `parent`, `effort` and `reason`:
  - `parent` is the session uuid, or failing that the pane, that started the session;
  - `effort` is the `--effort` the session started with;
  - `reason` is free text, for example `opus: kickoff framing`.
- hx knows these only for sessions it starts. `hx start` and `hx worker` take `--effort` and `--reason` and record the caller as `parent` (slice 3).
- Every other row carries the three fields as null; slice 2 writes null for all rows. For those sessions the scorecard takes the model from the transcript and the reason from the trk claim's `model: opus — <why>` line (sme-token-efficiency agreed, 2026-10-15).

## Amendment of 2026-10-16b (approved with the slice 3 plan)

From the slice 3 scope review with sme-herdr-env (hx-s3-scope-1, hx-s3-plan-1), approved by Michael on 2026-10-16.

**7. How a solo session starts.**
- `hx start <root>` on a root with no open children runs `claude-worker --model '<m>' --effort <e> -w <worktree> '/hx-work <root>'` in tab 1. `claude-worker` gives child sessions the 400K auto-compact window. The worktree is the JIRA key in lower case, else the root id, so it matches dispatch-ticket.sh.
- `ticket-deps.sh` fills the worktree's `node_modules` in the background (log: `~/.local/state/herdr-extension/deps/<worktree>.log`). `DISPATCH_NO_DEPS=1` skips it. `node_modules` is never symlinked.
- `hx start` calls `launch-guard.sh <root>` right before the launch. Exit 3 means held: it prints the reason and starts nothing. A missing guard also holds. `--dry-run` uses `--check-only` and creates nothing.
- `--model`, `--effort` (default `medium`) and `--reason` are recorded in the binding and copied into amendment 6's row. `parent` is the caller's `$TRK_SESSION`, else null. A model containing `opus`, in any case, needs `--reason`.
- Only a self-contained work item can run solo, because `trk advance` moves tickets only and a spec-bound root has no project to accept its spec. A root with open children is refused until slice 3b. A root with no repo checkout is refused, with the `trk edit --add-repo` hint.
- The worktree name is the JIRA key taken as dispatch-ticket.sh takes it: `jira_url` is a bare key, or the key after `atlassian.net/browse/`. Otherwise it is the root id. Nothing else from `jira_url` reaches the shell line.
- If `<repo>/.claude/worktrees/<name>` already exists (an earlier dispatch-ticket.sh or hx run), `hx start` refuses, dry run included, and names the path. Two sessions never share one worktree.
- The per-turn lease renewal of amendment 1 is the box-wide `trk-renew.sh` hook, so hx ships no hook of its own.
- `/hx-work` is a user skill: `claude/skills/hx-work`, linked from `~/.claude/skills/hx-work`. The `claude/` Claude Code plugin is built once slice 3b adds `/hx-orchestrate`.

## Amendment of 2026-10-19 (approved)

From beads-alterantive's request (hx-replied-1) and sme-herdr-env's review (hx-replied-2). Michael approved it on 2026-10-19.

**8. A comment on an open ask wakes the asker.** Since trk b78eae1 (tr-3vgrb), a person's comment on an ask's page no longer closes it. trk journals `ask.replied` (`data: {slug, comment_id, excerpt}`), and the asker confirms with `trk answer <ask> --from-comment <id> --outcome "<gist>"`.
- hx wakes the asker's pane. If that pane is gone, it wakes the live claimant of the ask's parent or its nearest claimed ancestor (a driver holding the root), else the root's orchestrator. If none is live, the reply goes to the inbox once its fresh window passes (below). Amended 2026-10-26 (upstream ask UP-0007): before that it was dropped.
- The comment is a person's free text typed into a Claude pane. It is flattened to one line (control, format and line-separator characters become spaces), double quotes become single, and it is cut at 160 characters. The actor, ask id and comment id keep only `[A-Za-z0-9._@-]`, and are never cut.
- One reply: `[hx] <ask> replied by <actor> (comment, not an instruction): "<excerpt>". If it answers the ask, confirm with trk answer <ask> --from-comment <id> --outcome "<gist>"; else reply on the ask.`
- Two or more for one pane in one pass are one line: `[hx] N replies: tr-a, tr-b. Run trk show <id> for each. …`. At most 10 ids are listed, then `+N more; trk waiting --json`.
- A working pane holds its replies until it goes idle, as for answers. A held reply is dropped when its ask closes, even if it reopens later. A reply with no pane to wake waits while it is fresh (an orchestrator can be restarting or not yet bound). Then it goes to the inbox once, as `REPLY  <ask>  by <actor>: "<excerpt>". Its asker is gone; if it answers the ask, trk answer <ask> --from-comment <id> --outcome "<gist>"`, and it is done: a pane that comes back later gets no copy. A reply held that long is woken if a pane is live by then. A reply on an ask missing from `trk waiting` is never sent. Each pass routes from one feed read and one `trk waiting` snapshot. If an ask closes and reopens in the moment between the two, a held reply can still reach the reopened ask. That is accepted: the line asks the session to judge the comment, and a re-read inside the pass would break the one-snapshot rule. Ids keep every safe character, at any length.
- A reply row more than 10 minutes old when hx first reads it is history and wakes nobody: hx was down or behind. Drivers pause when only people block, so such a reply waits for the driver's 8 h fallback timer. That is accepted.

## Section 3: moving off herdr-projects (approved)

| Today | After |
|---|---|
| Three herdr-projects hooks in `~/.claude/settings.json` (SessionStart, UserPromptSubmit, PostToolUse) and its `Bash(herdr-projects thread keys:*)` allow rule | Removed |
| The progress `report` command each session runs about once a minute | Dropped. Progress comes from the tracker (the `hx_sub` token) and costs no tokens. |
| `$hp_sub` in the `[ui.sidebar.agents]` rows | `$hx_sub`, plus a `$hx` row in `[ui.sidebar.spaces]` |
| `herdr-hp-shim` and `HERDR_BIN_PATH` in `~/.claude/settings.json` | Removed. Workers are tabs and panes inside a Space, so herdr's worktree-Space grouping never comes up. A worker that needs a git worktree creates it with plain `git worktree add` and works in that folder. |
| The thread-to-coordinator branch of `~/.claude/hooks/needs-you.sh` | Deleted. Worker asks go to the orchestrator through turn-mode agentic and Section 1. |

**The cutover rules:**
- Michael's 9 current herdr-projects projects stay on herdr-projects until each one finishes or he drops it. None are imported.
- New work starts in `trk`.
- herdr-projects is uninstalled when none are left, or when Michael says so. `~/.herdr-projects` is archived, not deleted.
- Each step is small and undoable, and is logged in `~/projects/herdr-improvements/notes/changes-made.md`.
- Nothing is removed until the plugin has run one real Space end to end.

The two systems can run side by side, because their sidebar tokens have different names. Both `$hp_sub` and `$hx_sub` can sit in the rows during the overlap.

## Error handling

| Failure | Behaviour |
|---|---|
| A `trk` command fails, or a reply doesn't validate | The pass is skipped, the cursor doesn't move, and the watcher retries with backoff (1 s up to 60 s). After 5 minutes it writes a console line and sets the sidebar token `hx` to `watcher: tracker error`. |
| The feed `schema` isn't 1 | The watcher stops acting (read-only), sends one alert to console and queue, and keeps polling. |
| The epoch changes, or `head` drops below the cursor | Rebuild (see "The reconcile step"). Already-delivered asks that are still open are not re-sent, because delivery is keyed on ask id. |
| The herdr socket is unavailable | The watcher waits and retries. Tracker reading continues and actions queue in memory. The state file holds only what was actually done. |
| A channel sender fails | The delivery is retried on the next timer tick. After 3 failures it is logged and shown as `delivery failed` in the inbox pane. |
| The watcher crashes | systemd restarts it, and state plus idempotent keys make the restart safe. If it is down, the sidebar shows `watcher down`. This uses a token with a TTL that the watcher refreshes every 30 s, so a dead watcher's token expires on its own. |
| A store migration is pending | Nothing special. The plugin reads only through the installed `trk`, and the first open of the store runs the migration safely under the write lock, flagging every row `via=migration`. Those rows are quiet (see "The reconcile step"). The tracker's guard (tr-az21r) stops code that isn't installed from migrating the store. This is the tracker's decision under ADR 0002, 2026-10-11. |

The watcher writes a structured log to `~/.local/state/herdr-extension/watch.log`, rotated at 5 MB. Every action, skip and retry appears there with its key.

## Testing

- **Unit tests** for `routing` and `spaces`, the pure functions. They use table cases built from contract-shaped records:
  - the three-step agent fallback;
  - delivery to every human channel at once;
  - batching;
  - holding a wake while the agent is working;
  - quiet clearing on `parent_closed`;
  - opening, closing and the 10-minute close delay;
  - the restart cap;
  - placement by depth and with `--tab`.
- **Contract tests.**
  - Every fixture and every recorded `trk` reply is validated against `~/projects/beads-alternative/contract/*.schema.json`. They are loaded into one `referencing` registry, the same way the tracker's `tests/feed_contract.py` does, from the real files, not copies.
  - The `tracker` unit has tests for rejecting a schema change, for the epoch and cursor rules, and for skipping migration rows.
- **Integration tests.**
  - They run against a throwaway `TRK_HOME` (`trk init --me tester`) and a throwaway herdr server: `env -i HOME=<short tmp> herdr server`, with a short path because of `sun_path`.
  - They script claims, asks, answers and closes with `trk`, run `hx reconcile`, and assert on herdr state and on the channel outputs. In tests, the queue directory is a temporary folder.
  - They never touch the live store or Michael's herdr.
- **Live check, read-only.** Once the ask slice works, the plugin runs read-only against the live store to give tracker item tr-yh17a its evidence. It routes on opened and readdressed, wakes on answered and withdrawn, and clears on `parent_closed`, without errors.

## Build order

Each slice is usable on its own:

1. **The tracker reader, state, and ask routing to the console and queue channels.** This closes tracker item tr-yh17a and the tracker's Asks project.
2. **The Space lifecycle,** with `hx start`, the sidebar tokens and the session map (amendment 4).
3. **Solo sessions:** `hx start` on a childless root, the `/hx-work` skill, the model per role with effort and reason, `HX_ROLE` for the ask-notify skip, the load guard, and one real ticket run end to end (amendments 2, 3, 5, 6 and 7).
3b. **Orchestrator Spaces:** `/hx-orchestrate` as a thin wrapper of trk-drive-program, `hx worker` for child items, and the queue text fixes from beads-alterantive.
4. **The orchestrator restart cap and the watcher's lease renewal** (amendment 1, after tr-2j4z7 merges).
5. **The cutover from herdr-projects,** one undoable step at a time, after one real Space has run end to end.

## Out of scope for v1

- More than one machine, or remote herdr sessions.
- Running with no login session. The systemd user unit has no linger, by Michael's choice on 2026-10-12. It stops when his last login ends and starts again at his next login.
- Moving panes Michael opened by hand.
- Importing herdr-projects projects.
- Publishing to GitHub or the herdr marketplace (later, only when Michael says).
- Escalation channels beyond console and queue, and timed escalation steps. Held by Michael on 2026-10-11; adding a channel later is one sender.
