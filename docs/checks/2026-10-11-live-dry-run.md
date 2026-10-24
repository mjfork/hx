# Live read-only check: slice 1 (ask routing), for tr-yh17a

Date: 2026-10-11. Branch `slice-1-ask-routing`. `hx` was installed with `uv tool install -e`. The watcher service was **not** installed or started.

## Tracker

- `trk --version --json`: 0.1.0 3dcd696 installed store_schema 5
- Live feed, read from seq 0: head 1428 epoch a5e812d8-cb52-4c44-a7a5-f33a1f4d39f9 rows 1428 ask rows 6
- Open asks (`trk waiting --all --json`): 0

## Dry run against the live store and the real herdr

Command: `HX_STATE_DIR=<temp dir> hx reconcile --dry-run`

- Exit 0, with no TrkError and no HerdrError. Every page of the live feed passed contract validation (`contract/changes.schema.json`), as did `waiting`, `claims` and herdr's `pane list`.
- Actions decided: none. That is correct. The live store has 6 ask rows, all for asks that are closed now, and no open asks. A first run reads them as history, so it delivers nothing and wakes nobody.
- The temporary state dir stayed empty, so the dry run wrote no state.

## The four ask row kinds (throwaway store, the real trk CLI)

`uv run pytest tests/test_reconcile.py tests/test_routing.py -v`:  18 passed in 11.82s 

| Row | Behaviour | Test |
|---|---|---|
| ask.opened to a human | a console line, plus a queue item on the asker's pane | test_ask_to_michael_reaches_console_and_queue |
| ask.opened to an agent | a wake for the claimant on the nearest ancestor, batched, held while the pane works | test_agent_ask_wakes_the_claimant_on_the_nearest_ancestor, test_two_asks_for_one_pane_are_one_message, test_wake_held_while_working |
| ask.readdressed | a new generation, so a new delivery | test_readdress_bumps_generation (tests/test_feed.py) |
| ask.answered | wakes the asker once, waits while it works, clears the queue | test_answer_wakes_the_asker_and_clears_queue, test_answer_wake_waits_until_the_asker_is_idle |
| ask.withdrawn, reason parent_closed | a quiet clear, with no wake | test_parent_closed_clears_quietly |
| history replay | old answers wake nobody, across every feed page | test_first_run_reads_every_page_as_history |

Full suite: 42 passed in 16.78s
