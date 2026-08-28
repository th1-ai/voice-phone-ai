---
name: voice-phone-ai
description: Run Voice / Phone AI ("The Switchboard") — Answers the phone 24/7 in the guest's own language — eight languages and a choice of voices on the demo line.. Use when the user asks to run the agent, check what is waiting for review, approve or reject a draft, or asks how the agent is doing. Trigger phrases: "run The Switchboard", "/voice-phone-ai", "check the queue", "what is waiting for me", "approve that draft".
---

# Voice / Phone AI

Runs Voice / Phone AI and works its review queue. Everything happens from the repo
root; every command below exists and works.

## Before anything else

Read `README.md` if you have not this session, and `workflows/10-*.md` for the
main loop. If the user has never run this agent, start at `workflows/00-setup.md`
instead and walk them through it.

## The loop

**1. Check the agent is healthy.**

```bash
make doctor
```

Any `FAIL` line has a fix hint. Fix it before going further. `WARN` lines are
worth mentioning but do not stop the run.

**2. Run one pass.**

```bash
make run                        # one pass over new work
make run ARGS="--limit 5"       # just the first five items
make run ARGS="--dry-run"       # compute everything, write nothing
```

If `llm.provider` is `interactive`, the run will stop with exit code 3 and park
prompts in `data/pending/`. That is expected. Read each `*.prompt.md`, write your
answer as JSON to the matching `*.answer.json` following the schema exactly, then
run the same command again.

**3. Show what is waiting.**

```bash
make review
python3 tools/review.py show <id>
```

Summarise it for the user in plain language: who wrote in, what the agent thinks
it is, what it drafted, and how confident it was. Do not paste raw JSON at them.

**4. Act on their decision.**

```bash
python3 tools/review.py approve <id>
python3 tools/review.py edit <id> --body-file <path>
python3 tools/review.py reject <id> --reason "<why>"
```

Read the draft back to them before approving. If they want changes, write the new
version to a file and use `edit` — the before/after is stored and is what teaches
the agent their voice.

**5. Report.**

```bash
make report
```

## Rules

- **Never send in shadow mode**, and never work around a blocked write. The error
  message says what to do.
- **Going live is the hotel's decision.** Only raise it after
  `workflows/90-go-live.md` has been worked through.
- **Confirm before anything irreversible** — a guest email, a PMS write, a
  payment — even when it is approved.
- **Never print or paste a credential.**
- If a run fails, read the whole error, fix the cause, re-run, and note what you
  learned in `workflows/99-troubleshooting.md`.
