# Workflow: troubleshooting

Read the whole error before doing anything - every tool here is written to
say what broke and what to do about it. If you fix something not covered
below, add it here.

## `make doctor` shows a FAIL

Each `FAIL` line has a `->` fix hint right under it. Common ones:

- **`hotel identity`: name is still 'Hotel Aurora'.** Expected on a fresh
  clone. Edit `config/hotel.yaml`.
- **`room types`: no rooms.room_types in config/agent.yaml.** Copy
  `config/agent.example.yaml` to `config/agent.yaml` and replace the sample
  rooms with your own.
- **`llm provider`: claude-code selected but `claude` is not on PATH.**
  Install Claude Code, or switch `llm.provider` to `interactive` or
  `anthropic` in `config/hotel.yaml`.
- **`llm provider`: ANTHROPIC_API_KEY is not set.** Add it to `.env`, or
  switch `llm.provider` to `claude-code` or `interactive`.
- **An adapter shows FAIL, not warn.** `universal`/`built` adapters fail
  loud when misconfigured (a `warn` is reserved for the mock adapter and
  for stubs). Read the `detail` column - it names the missing file or
  variable.

## `make demo` does not print `DEMO OK`

- Make sure `make setup` ran first (`.venv` must exist).
- `tools/demo.py` forces `llm.provider=mock` and reads
  `fixtures/inbound/*.json` and `fixtures/hotel/reservations.json` - if you
  deleted or renamed those files, restore them from git.
- Read the traceback if there is one; `tools/demo.py` does not swallow
  errors on purpose, so a fixture problem shows up immediately.

## `make run` exits with code 3

Not an error. `llm.provider: interactive` parked a prompt. Read
`data/pending/*.prompt.md`, write your answer to the matching
`*.answer.json` (JSON only, matching the schema shown, no prose, no code
fence), and run the same command again.

## `make run`/`make doctor` prints a clear message, then `make: *** [run] Error 1`

Harmless. That second line is `make` itself reporting that the recipe it ran
exited non-zero - it always adds it, for any non-zero exit, on top of
whatever the tool already printed. It is not a second, separate failure.
Read the tool's own message above it (that is the real one); if you want to
avoid the `make` banner entirely, run the underlying command directly, e.g.
`.venv/bin/python tools/run.py --once` instead of `make run`.

## An item is stuck at `sending`

A process died between claiming an item and finishing the send.
`tools/run.py` and `tools/review.py send` both call
`core.store.Store.reap_stuck_sending()` on their next pass, which moves
anything stuck for more than 30 minutes to `failed` so you see it in the
queue instead of it vanishing. Use `python3 tools/review.py retry <id>` once
the cause is fixed.

## A room, table, room-service or guest-request callback gets approved but nothing was written

Check `python3 tools/review.py show <id>` for the item's `error` field
first - `tools/review.py send` records exactly why `finalize_action` failed
(a guardrail refusal, or the original preview having already rejected the
request) rather than silently dropping the booking.

## "no callback channel captured on this call"

The caller left no email and no phone number in the transcript (or the
transcript did not say so clearly enough for classify to extract one). Call
them back using whatever number your phone system shows, then
`python3 tools/review.py reject <id> --reason "called back manually"` - see
`workflows/80-review.md` step 5.

## The classify or draft step gets a call wrong

Fix it in the review queue first (`edit`, not `reject`, so the correction is
recorded as a learning), then update `knowledge/policies.md` or
`config/agent.yaml` if the pattern is likely to repeat. Prompts are plain
markdown - `prompts/classify.md` and `prompts/draft.md` can be edited
directly too.

## `python3 tools/*.py` says `ModuleNotFoundError: No module named 'core'`

You ran it with a Python that is not the repo's virtualenv, or from outside
the repo root. Use `make run` / `make doctor` / etc. (they call
`.venv/bin/python` for you), or run `.venv/bin/python tools/run.py` directly
from the repo root.

## I do not have a real telephony provider yet - can I still use this?

Yes. Read `docs/how-it-works.md` ("What this repo actually is") and
`docs/integrations.md` ("Telephony - the honest gap") - this repo is
useful the moment you have *any* way to get a call transcript into a
mailbox (a voicemail-to-text service, a call-recording add-on, or a staff
member typing up a summary). Adding real-time answering is a separate,
later project that needs a telephony provider.

## Still stuck

`data/logs/*.jsonl` has every decision the agent made, in order, with a run
id. `python3 tools/review.py show <id>` has the full event trail for one
item. If neither explains it, that is a real bug - describe exactly what you
ran and what you expected, and ask.
