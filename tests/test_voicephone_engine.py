"""Tests for tools/engine.py - the whole classify -> preview -> draft pass."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.adapters import get_email, get_pms
from core.config import load_settings
from core.store import Store

import store_ext
from engine import apply_language_gate, needs_human_for, process_call
from booking import BookingOutcome


def _settings(mode="shadow"):
    return load_settings(demo=True, mode=mode)


def _store(tmp_path, name="engine.db"):
    store = Store(load_settings(provider="mock", mode="shadow"), path=tmp_path / name)
    store_ext.ensure_schema(store)
    return store


def test_process_call_room_booking_happy_path(tmp_path):
    settings = _settings()
    store = _store(tmp_path)
    pms = get_pms(settings)
    email = get_email(settings)
    msg = next(m for m in email.fetch_unread(limit=50) if m.id == "call-01")
    item, did_work = process_call(settings, store, pms, msg, provider="mock")
    assert did_work is True
    assert item.intent == "room_booking"
    assert item.review_status == "pending_review"
    assert item.draft["channel"] == "email"


def test_process_call_sold_out_room_is_still_pending_review_not_needs_human(tmp_path):
    settings = _settings()
    store = _store(tmp_path)
    pms = get_pms(settings)
    email = get_email(settings)
    msg = next(m for m in email.fetch_unread(limit=50) if m.id == "call-02")
    item, _ = process_call(settings, store, pms, msg, provider="mock")
    assert item.review_status == "pending_review"
    assert item.draft["pending_booking"]["ok"] is False


def test_process_call_large_group_needs_human(tmp_path):
    settings = _settings()
    store = _store(tmp_path)
    pms = get_pms(settings)
    email = get_email(settings)
    msg = next(m for m in email.fetch_unread(limit=50) if m.id == "call-03")
    item, _ = process_call(settings, store, pms, msg, provider="mock")
    assert item.review_status == "needs_human"


def test_process_call_no_action_is_skipped_with_no_draft(tmp_path):
    settings = _settings()
    store = _store(tmp_path)
    pms = get_pms(settings)
    email = get_email(settings)
    msg = next(m for m in email.fetch_unread(limit=50) if m.id == "call-12")
    item, did_work = process_call(settings, store, pms, msg, provider="mock")
    assert did_work is True
    assert item.review_status == "skipped"
    assert item.draft is None


def test_process_call_escalation_needs_human(tmp_path):
    settings = _settings()
    store = _store(tmp_path)
    pms = get_pms(settings)
    email = get_email(settings)
    msg = next(m for m in email.fetch_unread(limit=50) if m.id == "call-10")
    item, _ = process_call(settings, store, pms, msg, provider="mock")
    assert item.review_status == "needs_human"
    assert item.draft["ai_suggested_reply"]


def test_process_call_is_idempotent_on_a_second_pass(tmp_path):
    settings = _settings()
    store = _store(tmp_path)
    pms = get_pms(settings)
    email = get_email(settings)
    msg = next(m for m in email.fetch_unread(limit=50) if m.id == "call-04")
    item1, did_work1 = process_call(settings, store, pms, msg, provider="mock")
    item2, did_work2 = process_call(settings, store, pms, msg, provider="mock")
    assert did_work1 is True
    assert did_work2 is False
    assert item1.id == item2.id


def test_apply_language_gate_forces_default_language_and_escalates():
    settings = load_settings(provider="mock", mode="shadow")
    classification = {"language": "de", "call_type": "question", "confidence": 0.9}
    escalation = apply_language_gate(classification, settings)
    assert escalation is not None
    assert classification["language"] == settings.hotel.default_language


def test_apply_language_gate_leaves_a_supported_language_alone():
    settings = load_settings(provider="mock", mode="shadow")
    lang = settings.hotel.languages[0]
    classification = {"language": lang, "call_type": "question", "confidence": 0.9}
    assert apply_language_gate(classification, settings) is None
    assert classification["language"] == lang


def test_needs_human_for_confidence_threshold():
    settings = load_settings(provider="mock", mode="shadow")
    ok = BookingOutcome(True, "none")
    below = {"call_type": "question", "confidence": 0.1, "escalation": None,
             "missing_info": []}
    above = {"call_type": "question", "confidence": 0.99, "escalation": None,
             "missing_info": []}
    assert needs_human_for(below, ok, {}, settings) is True
    assert needs_human_for(above, ok, {}, settings) is False


def test_needs_human_for_trusts_the_preview_flag():
    settings = load_settings(provider="mock", mode="shadow")
    flagged = BookingOutcome(True, "room", needs_human=True)
    classification = {"call_type": "room_booking", "confidence": 0.99, "escalation": None,
                      "missing_info": []}
    assert needs_human_for(classification, flagged, {}, settings) is True


def test_interactive_provider_resumes_at_draft_without_re_asking_classify(tmp_path):
    """The interactive provider needs two separate answers per call
    (classify, then draft) - a second run must not lose the first answer,
    and must not ask classify again while waiting on draft. See the
    docstring on tools/engine.py:process_call."""
    import json

    from core.adapters.base import EmailMessage
    from core.llm import LLMPendingInteractive

    settings = load_settings(provider="interactive", mode="shadow")
    store = Store(settings, path=tmp_path / "interactive.db")
    store_ext.ensure_schema(store)
    pms = get_pms(settings)
    msg = EmailMessage(id="test-interactive-1", subject="Parking question",
                       body_text="Do you have parking?",
                       received_at="2026-08-20T08:00:00+00:00")

    pending_dir = settings.root / "data" / "pending"
    classify_answer = pending_dir / "classify-test-interactive-1.answer.json"
    draft_answer = pending_dir / "draft-test-interactive-1.answer.json"
    for leftover in pending_dir.glob("*test-interactive-1*"):
        leftover.unlink()
    try:
        try:
            process_call(settings, store, pms, msg)
            assert False, "expected LLMPendingInteractive"
        except LLMPendingInteractive as exc:
            assert exc.pending_id == "classify-test-interactive-1"

        classify_answer.write_text(json.dumps({
            "call_type": "question", "language": "en", "confidence": 0.95,
            "caller": {"name": None, "phone": None, "email": None}, "reservation_ref": None,
            "booking": None, "guest_request": None, "missing_info": [], "escalation": None,
            "reason": "parking question"}), encoding="utf-8")
        try:
            process_call(settings, store, pms, msg)
            assert False, "expected LLMPendingInteractive"
        except LLMPendingInteractive as exc:
            assert exc.pending_id == "draft-test-interactive-1"
        item = store.get_by_external("call", "test-interactive-1")
        assert item.intent == "question"
        assert "_classify_cache" in item.payload

        draft_answer.write_text(json.dumps({
            "subject": "Parking", "body": "We do not have parking on site.",
            "needs_human": False, "ai_suggested_reply": None}), encoding="utf-8")
        item, did_work = process_call(settings, store, pms, msg)
        assert did_work is True
        assert item.review_status == "pending_review"
        assert item.draft["body"] == "We do not have parking on site."

        item, did_work = process_call(settings, store, pms, msg)
        assert did_work is False
    finally:
        for leftover in pending_dir.glob("*test-interactive-1*"):
            leftover.unlink()
        store.close()
