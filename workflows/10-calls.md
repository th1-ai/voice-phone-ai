# Workflow: the calls loop

Objective: run one pass over unread call transcripts and see what Voice /
Phone AI did with each one.

## Inputs

- A configured `systems.email.adapter` (`mock` by default - see
  `workflows/00-setup.md` step 6 to connect a real mailbox).
- `config/agent.yaml`'s `confidence_threshold`, `rooms`, `restaurant` and
  `room_service` blocks - the defaults (Hotel Aurora's own room ladder and
  menu) work for a dry run; replace them with your property's real ones
  before going live.
- `config/hotel.yaml`'s `systems.pms.adapter` for a genuine availability
  check (`mock` by default, reading `fixtures/hotel/reservations.json`).

## Steps

1. **Run one pass.**
   ```bash
   make run
   make run ARGS="--limit 5"       # just the first five call transcripts
   make run ARGS="--dry-run"       # compute everything, write nothing
   ```
   Every new call transcript is classified (`prompts/classify.md`) into a
   `call_type`, then the relevant booking is previewed against your real
   room types, restaurant hours and room-service menu
   (`tools/booking.py`, `tools/pricing.py` - no model call), then a callback
   is drafted (`prompts/draft.md`). See `docs/how-it-works.md` for the full
   flowchart.

2. **If `llm.provider` is `interactive`,** the run stops with exit code 3
   and parks a prompt in `data/pending/`. Read `*.prompt.md`, write your
   answer as JSON to the matching `*.answer.json` exactly matching the
   schema shown, and run the same command again. Do this for classify and
   then again for draft, for each call - unless the call turns out to be
   `no_action` (a wrong number, silence, spam), which needs no draft at all.

3. **See what happened.**
   ```bash
   make review
   ```
   A routine booking, order, request or question above
   `confidence_threshold` is `pending_review`. Anything that trips a
   guardrail in `knowledge/policies.md` - a complaint, a missing detail, an
   unconfirmed room choice, a large party or group, or low confidence - is
   `needs_human`, on purpose (`docs/safety.md`).

4. **Work the queue.** `workflows/80-review.md` covers approve / edit /
   reject / send in full. Approving and sending a room, table, room-service
   or guest-request callback is also the moment the real booking row is
   written - see `docs/how-it-works.md` design decision 3.

5. **Keep it running.**
   ```bash
   make watch                       # loop on the configured interval
   ```
   Or schedule it - `scheduler/` has cron, launchd and systemd examples.
   `config/agent.yaml`'s `schedule.calls` documents the interval this repo
   was built around (every 10 minutes).

## Edge cases

- **No new call transcripts.** `make run` prints `0 items processed, 0
  drafted, 0 sent` and exits 0. Nothing to do.
- **A wrong number, silence, or spam.** Classified `no_action` and moved
  straight to `skipped` - no draft, no callback, nothing in the review
  queue for it.
- **A transcript the model cannot answer cleanly.** `core.llm` raises
  `LLMSchemaError` rather than accept a bad answer; the item is queued as
  `needs_human` with the error recorded, instead of guessing.
- **A table request lands on a closed day, or a room type is sold out.**
  This is a normal callback offering an alternative, not automatically a
  human matter - see `tools/engine.py:needs_human_for`.
- **A room or table request is oversized** (guests or party size over the
  thresholds in `config/agent.yaml`). Always escalates, even though the
  booking itself is technically valid.
- **A caller left no email and no phone number.** The callback still gets
  drafted (as a script for a person to read straight off the screen when
  they call back), but `tools/review.py send` cannot deliver it
  automatically - see `workflows/80-review.md`.
- **A re-run sees the same call again.** `tools/engine.py` skips anything
  the store has already seen - see `core.store.Store.upsert_item`.
