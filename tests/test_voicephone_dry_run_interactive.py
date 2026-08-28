"""Regression test for SIMULATION.md finding 3 (2026-08-27).

`make run ARGS="--dry-run"` uses a fresh, in-memory `Store` every
invocation (`tools/run.py`: `path=":memory:" if settings.dry_run else
None`), so nothing survives between two dry-run passes except the files
under `data/pending/`. Before this fix, `core.llm._interactive` renamed a
consumed answer file to `*.json.used` on every pass, including a dry run -
so a hotel rehearsing a multi-step call (every call type except
`no_action`/a plain `question` with nothing missing) had to re-answer the
same `classify` prompt on every single invocation, because the answer that
would let it resolve automatically had already been consumed and deleted.

The fix: `core.llm.complete` only consumes the answer file when
`not settings.dry_run` (see the `consume=` argument threaded through to
`_interactive`). Because `core.llm._pending_id` is deterministic (task +
fixture_id, or a hash of the prompt when there is no fixture_id), a second
dry-run invocation resolves the SAME `classify-<id>.answer.json` again
without asking, and progresses to the next step (`draft`) instead of
re-asking `classify` - matching `docs/how-it-works.md`'s "Idempotency"
promise even under `--dry-run`.

This test simulates three separate `tools/run.py --once --dry-run`
invocations by building a brand-new `Store(settings, path=":memory:")`
each time (a real process restart discards the in-memory store the same
way), and checks the interactive answer file for `classify` is still on
disk - and still usable - after invocation 2.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.adapters import get_pms
from core.adapters.base import EmailMessage
from core.config import load_settings
from core.llm import LLMPendingInteractive
from core.store import Store

import store_ext
from engine import process_call

CALL_ID = "test-dryrun-interactive-1"

CLASSIFY_ANSWER = {
    "call_type": "question", "language": "en", "confidence": 0.95,
    "caller": {"name": None, "phone": None, "email": None}, "reservation_ref": None,
    "booking": None, "guest_request": None, "missing_info": [], "escalation": None,
    "reason": "parking question",
}
DRAFT_ANSWER = {
    "subject": "Parking", "body": "We do not have parking on site.",
    "needs_human": False, "ai_suggested_reply": None,
}


def _memory_store(settings) -> Store:
    """A brand-new in-memory store - what tools/run.py builds on every
    `--dry-run` invocation. Building a fresh one each call is exactly what
    a second `python3 tools/run.py --once --dry-run` process does: the
    previous store is gone."""
    store = Store(settings, path=":memory:")
    store_ext.ensure_schema(store)
    return store


def test_dry_run_interactive_progresses_across_invocations(tmp_path):
    settings = load_settings(provider="interactive", mode="shadow", dry_run=True)
    assert settings.dry_run is True
    pms = get_pms(settings)
    msg = EmailMessage(id=CALL_ID, subject="Parking question",
                       body_text="Do you have parking?",
                       received_at="2026-08-20T08:00:00+00:00")

    pending_dir = settings.root / "data" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    classify_answer = pending_dir / f"classify-{CALL_ID}.answer.json"
    draft_answer = pending_dir / f"draft-{CALL_ID}.answer.json"
    for leftover in pending_dir.glob(f"*{CALL_ID}*"):
        leftover.unlink()

    try:
        # --- invocation 1: nothing answered yet -> pends on classify -------
        store1 = _memory_store(settings)
        try:
            try:
                process_call(settings, store1, pms, msg)
                assert False, "expected LLMPendingInteractive"
            except LLMPendingInteractive as exc:
                assert exc.pending_id == f"classify-{CALL_ID}"
        finally:
            store1.close()

        classify_answer.write_text(json.dumps(CLASSIFY_ANSWER), encoding="utf-8")

        # --- invocation 2: brand-new store (process "restarted") -----------
        # This is finding 3's exact reproduction. Before the fix, a dry run
        # consumed the answer file just like a real run, so this second
        # invocation would find no answer and pend on classify AGAIN -
        # forcing the hotel to re-supply an identical answer forever.
        assert classify_answer.exists(), (
            "a dry run must not consume the interactive answer file - "
            "otherwise no later invocation can ever resolve classify")
        store2 = _memory_store(settings)
        try:
            try:
                process_call(settings, store2, pms, msg)
                assert False, "expected LLMPendingInteractive"
            except LLMPendingInteractive as exc:
                # progressed PAST classify to draft - not re-asking classify.
                assert exc.pending_id == f"draft-{CALL_ID}", (
                    f"expected to progress to draft, but got "
                    f"{exc.pending_id!r} - classify was asked again, so "
                    f"finding 3 has reopened")
        finally:
            store2.close()

        # the classify answer survives a second read too - a hotel can run
        # `--dry-run` as many times as it likes while only draft is missing.
        assert classify_answer.exists()

        draft_answer.write_text(json.dumps(DRAFT_ANSWER), encoding="utf-8")

        # --- invocation 3: both answers present -> computes end to end -----
        store3 = _memory_store(settings)
        try:
            item, did_work = process_call(settings, store3, pms, msg)
            assert did_work is True
            assert item.review_status == "pending_review"
            assert item.draft["body"] == "We do not have parking on site."
        finally:
            store3.close()

        # a dry run never writes to the REAL (non-memory) database - see
        # factory/workflows/build-repo.md section 5 ("--dry-run writes
        # nothing"). Three dry-run passes above must have left it empty.
        real_db = Store(settings, path=settings.db_path())
        try:
            assert not real_db.counts(), (
                "a --dry-run pass wrote to the real database - it must "
                "only ever use the in-memory store")
        finally:
            real_db.close()
    finally:
        for leftover in pending_dir.glob(f"*{CALL_ID}*"):
            leftover.unlink()
