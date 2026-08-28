# Measuring the benefit

## The promise

**Output.** Captures the 20-40% of calls that currently go unanswered - with
the bookings, table reservations and orders they contain written into your
systems live.

**ROI.** +30% Calls captured (revenue)

Those numbers come from the demo platform's roster and describe the
promise of a fully live phone line - a real-time voice agent answering
every call as it happens. **This template does not answer a live call** -
see `docs/how-it-works.md`. What it gives you today is the second half of
that promise: every finished call transcript you can get into a mailbox
gets read, classified and turned into a queued callback with a genuine
availability check behind it, instead of a note nobody follows up on. The
"+30% calls captured" figure only becomes true once you add a telephony
provider on top - `docs/integrations.md` says exactly what that needs.
`make report` is how you find out what is actually true for your property.

## What to track

| Metric | Where it comes from | What it tells you |
|---|---|---|
| Volume by call_type and status | `store.counts()` via `tools/report.py` | how many calls are landing, and how much is still waiting on a person |
| Ledger totals | `room_bookings` / `table_bookings` / `room_service_orders` / `guest_requests` | what has actually been written - only populated once a send has gone through (`docs/how-it-works.md` design decision 3) |
| Edit rate | edited vs. approved-unchanged, from `learnings` | how often a person has to rewrite the callback rather than approve it as drafted |
| Time to first draft | call transcript landing -> callback ready for a human | how quickly a caller who left a message gets a response queued, bounded by `schedule.calls`, not by how fast a human happens to check the queue |
| Spend | `core.llm`'s usage logging | LLM calls, tokens, and cost - `0.00` is expected and correct on `mock`, `interactive` or `claude-code`; only `anthropic` bills per token |

Run it any time:

```bash
make report
python3 tools/report.py --json     # for a dashboard or a spreadsheet import
```

## Reading these numbers honestly

This repo ships in `mode: shadow` with `autonomy: draft` - every callback,
every booking, every order and every request waits for a human before
anything leaves the building or gets written. Nothing here auto-sends, ever
(there is no `autonomy: send` path in this repo - see
`config/agent.example.yaml`), so the closest thing to a volume metric is
"how many calls reached a queued, ready-to-approve callback" rather than
"how many were handled end to end with no human touch." A **falling edit
rate** and a **falling time to first draft** are the honest leading
indicators of the agent getting more useful for your property, not a rising
auto-handled percentage - there isn't one.

## The "calls that used to go unanswered" case

The roster's output line ("captures the 20-40% of calls that currently go
unanswered") is fundamentally about calls a hotel never even hears about
today - the ones that ring out after hours, or that leave a voicemail
nobody checks until the morning. This repo's honest contribution to that
number is: **every voicemail or call-recording transcript you can get into
a mailbox now gets read and acted on**, on the schedule you set
(`config/agent.yaml: schedule.calls`), rather than sitting until a person
happens to check it. Comparing "callbacks queued per week" against "calls
your phone log shows as missed" for the same period is the simplest way to
see whether that is closing the gap for your property - there is no
synthetic number this repo can print that substitutes for that comparison.

## Caveats, plainly

- Numbers are only as good as `knowledge/`. A property that has not filled
  in `policies.md`, `property.md` and `faq.md` with real facts will see a
  higher edit rate and more `needs_human` items than a well-tuned property -
  that is the system working correctly, not underperforming.
- `time_to_first_draft` measures transcript-to-draft, not draft-to-send. A
  drafted, unreviewed queue does not help a caller who is waiting; pair this
  metric with how often someone actually works `workflows/80-review.md`.
- The ledger tables only count what has been **finalized** - approved and
  sent. A large `pending_review`/`needs_human` count next to small ledger
  totals means the review queue, not the agent, is the current bottleneck.
- `spend` only ever reflects the `anthropic` provider. Choosing
  `interactive` or `claude-code` to run on a subscription instead is a
  deliberate cost decision covered in `docs/safety.md` - `tools/report.py`
  will correctly show USD 0.00 in that case, which is not the same as "no
  cost".
