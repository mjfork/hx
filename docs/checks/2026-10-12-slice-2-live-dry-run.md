# Slice 2 live check (2026-10-12)

trk 0.1.0 (commit 9731760), store schema 6, feed head 1931. At check time the live store had no claims and no open asks, so `hx reconcile --dry-run` on the live store exited 0 with no actions.

## Live herdr, throwaway trk store (dry run)

A temporary `TRK_HOME` held project `tr-6wps4` with ticket `tr-r28sb` (JIRA `PROJ-42`). The ticket was claimed by this session (`5a89e02a…`, live in pane `wB9:p3`), and the root by `gone-session` (in no pane, seeded into `seen`). Decided effects against the real herdr:

```
Report  ws   wB9     "tr-6wps4 · project · 1w"
Report  pane wB9:p3  "tr-r28sb ac 0/1"
Release gone-session
MapRow  5a89e02a… tr-r28sb worker ticket=PROJ-42 model=null pane=wB9:p3
```

No `Close` (the Space was adopted, never hx-opened). No release for the session that is in a pane.

## Live herdr calls

- Found a bug: `report-metadata` needs the target before the options. herdr answered `unknown option: hx`. Fixed with a test that failed first, and a 30-minute token TTL was added (as herdr-projects does).
- Set and cleared the `hx` and `hx_sub` tokens on `wB9` and `wB9:p3`.
- create (`--no-focus --label --cwd --env`), tab rename, pane run (the env reached the shell: `HX_ROLE=solo`), and close: all worked on a throwaway workspace that was then closed.

Not exercised live: `hx start` end to end, because it launches a Claude session. Its slice 3 skills do not exist yet.

## After the final-review fixes (commit e1d6a5d)

Same probe, run from a shell whose `HERDR_SOCKET_PATH` pointed at nothing. Every herdr call now passes `--session default`, so it still reached the live default server. The decided effects were the same: an ws token on `wB9`, a pane token on `wB9:p3`, a `Release` for `gone-session` (its pane `wZZ:p1` was not in herdr), and one map row with `ticket=PROJ-42`. The suite has 105 passing tests.
