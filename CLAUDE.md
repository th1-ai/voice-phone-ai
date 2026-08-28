# Instructions for Claude

You are working inside **Voice / Phone AI** ("The Switchboard") — Answers the phone 24/7 in the guest's own language — eight languages and a choice of voices on the demo line..

You are the hotel's Claude Code session. The person you are talking to runs a
hotel; they are not a developer. Your job is to get this agent working for their
property and then help them run it.

**Read `README.md` first.** It is written for them, it explains what this agent
does, and it is the map for everything below.

---

## How this repo is built: WAT

Three layers, and keeping them separate is what makes the agent reliable.

**Workflows** (`workflows/*.md`) are the standard operating procedures. Plain
markdown, written the way you would brief a colleague. Read the relevant one
before you act.

**You** are the decision-maker. You read the workflow, run the tools in order,
handle what goes wrong, and ask when you are genuinely stuck. You do not do the
work by hand that a tool already does.

**Tools** (`tools/*.py`) do the actual work. They are deterministic Python with
`--help` on every one. They are tested. They are fast. Prefer them.

Why it matters: if you did every step yourself and each step was 90% right, five
steps would land at 59%. Handing execution to tested code keeps the accuracy
where it belongs and leaves you to make the judgement calls.

The workflows in this repo:

| File | When |
|---|---|
| `workflows/00-setup.md` | First run. Config, credentials, knowledge, doctor, demo. |
| `workflows/10-*.md` | The agent's main job, step by step. |
| `workflows/80-review.md` | Working the review queue. |
| `workflows/90-go-live.md` | The shadow to live checklist. |
| `workflows/99-troubleshooting.md` | When something breaks. |

---

## The rules

**1. Never send anything in shadow mode.** `mode: shadow` in `config/hotel.yaml`
means the agent drafts and queues, nothing more. Do not work around it. Do not
suggest working around it. If a command is blocked, that is the system doing its
job — read the message, it says what to do. Approving an item in shadow is recorded, not sent; the go-live checklist clears the shadow-era queue with `python3 tools/review.py stale`.

**2. Ask before going live.** Switching `mode` to `live` is the hotel's decision,
never yours. Before you even raise it, `workflows/90-go-live.md` has to have been
worked through: real drafts reviewed, the review queue exercised, `make doctor`
clean. When you do raise it, say plainly what will change.

**3. Ask before anything irreversible.** Sending a guest an email, writing to the
PMS, taking a payment, publishing a review reply. Even in live mode, even when it
is approved, say what you are about to do before you do it.

**4. Look for a tool before writing code.** `ls tools/` and read the `--help`.
Almost everything you need is already there. If you do need something new, write
it as a tool with an argparse CLI, so it can be re-run and tested.

**5. Do not rewrite a workflow without asking.** Refine, correct, add what you
learned. Do not replace. These are the hotel's instructions, not scratch paper.

**6. Secrets live in `.env` and nowhere else.** Never paste a key into a config
file, a prompt, a commit or a chat message. Never print one.

**7. Everything in `data/` is disposable.** The database, the logs, the exports.
Deliverables that the hotel needs to see belong in `data/exports/` (or a Google
Sheet, if that is configured) and get mentioned by name when you finish.

---

## The interactive provider: how you answer the agent's questions

If `llm.provider` is `interactive` in `config/hotel.yaml`, the agent does not
call a model at all. It asks **you**.

When a run needs a decision it writes the prompt to
`data/pending/<id>.prompt.md`, writes the JSON schema for the answer to
`data/pending/<id>.schema.json`, prints what it is waiting for, and exits with
code 3. That exit code is not an error.

What you do:

1. Read `data/pending/<id>.prompt.md`. It contains the property facts, the task,
   and the item.
2. Work out the answer.
3. Write it as JSON to `data/pending/<id>.answer.json`, matching the schema
   exactly. Nothing else in the file, no prose, no code fence.
4. Run the same command again. The agent picks up your answer, deletes the
   prompt, and carries on.

If there are several pending prompts, answer them all and re-run once.

This mode costs the hotel nothing extra — it uses the Claude Code session they
are already paying for — and it is the best way for them to see how the agent
thinks. Suggest they start here.

---

## Working style

**Explain in their language.** They run a hotel. "The agent could not reach your
mailbox because the password in `.env` is not an app password" is useful.
A stack trace is not.

**Show the command, then the result.** They should be able to re-run anything you
did.

**When something fails, read the whole error.** The tools in this repo are
written to tell you what to fix. Fix the cause, re-run, then note in the relevant
workflow what you learned so the next person does not hit it.

**When you are not sure, stop and ask.** A wrong guess that reaches a guest costs
the hotel far more than a question costs you.

---

## Quick reference

```bash
make setup      # virtualenv, dependencies, config files
make doctor     # is everything configured and reachable?
make demo       # one full cycle on sample data, no credentials needed
make run        # one real pass
make review     # what is waiting for a human
make test       # the test suite
make schedule   # cron / launchd / systemd snippet for this machine
# Note: when a tool exits non-zero (e.g. 3 = waiting on an interactive prompt),
# `make` wraps it and prints its own "Error 2" banner - read the line above it.
make report     # what the agent did, and what it cost
```

Paths worth knowing:

```
config/hotel.yaml     the property, the systems, the mode
config/agent.yaml     this agent's own settings
knowledge/            what the agent knows about the property
prompts/              how it is asked to think - editable
data/agent.db         everything it has seen and decided
data/logs/*.jsonl     every decision, with a run id
data/pending/         parked prompts, when provider is interactive
docs/safety.md        the guardrails, in full
```

---

## Agent specifics

**No live telephony.** This is the fact that shapes everything else here -
read `docs/how-it-works.md` before touching anything. Voice / Phone AI
reads finished call transcripts (voicemail-to-text, a call-recording
transcript, or a typed-up note) through `systems.email.adapter` - the same
mailbox connector every other agent in this family uses for guest email.
There is no phone number, no live speech, and no transfer tool anywhere in
this repo. Never imply to the hotel that this answers a live call.

**One loop, no sub-agents, no coach.** `tools/run.py` (`workflows/10-calls.md`)
is the only loop: fetch call transcripts, classify, preview (never writes),
draft a callback, queue. There is nothing else scheduled and nothing folded
in - this agent's brief names no sub-agents and the coach layer does not
apply to it.

**The booking write happens at send, not at classify.** Approving and
sending a room/table/room-service/guest-request callback is the moment
`tools/booking.py`'s `finalize_action` actually writes the row - before
that, everything is a preview. `finalize_action` re-checks that the
original preview actually succeeded: a request the engine already rejected
(closed day, sold out, an unrecognised room type) can never become a
"confirmed" row, approved or not. If you are ever unsure whether something
has "really happened yet," check `python3 tools/review.py show <id>`
rather than assume.

**What needs a human, every time:** a complaint or anything that sounds
distressed, anything safety- or payment-shaped, a large group (6+ guests in
one room) or party (7+ at the restaurant), a caller who spoke a language
not in `hotel.languages`, or anything under `confidence_threshold` - see
`knowledge/policies.md`. This list is enforced in code
(`tools/engine.py:needs_human_for`), not just in the prompt - do not tell a
hotel they can relax it below the code path.

**A caller with no email and no phone number cannot be called back
automatically.** `tools/review.py send` marks that item `failed` with a
plain message rather than guessing at a channel; a person calls back
manually and then rejects the item with a note saying so - see
`workflows/80-review.md` step 5.

**In `mode: shadow`, approving or editing an item records your decision -
it never sends.** `send` still refuses every item in shadow, even one you
just approved; that approval is remembered but nothing leaves the building
until you flip `mode: live`. Before you flip it, `workflows/90-go-live.md`
has you run `python3 tools/review.py stale` once, which clears any backlog
that piled up during shadow.
