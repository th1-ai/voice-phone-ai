"""Tests for tools/review.py - the queue, and the shadow-mode send guard."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.config import load_settings
from core.review import approve
from core.store import Store

import store_ext
from review import cmd_send


def _settings(mode="shadow"):
    return load_settings(provider="mock", mode=mode)


def _store(tmp_path, name="review.db"):
    store = Store(_settings(), path=tmp_path / name)
    store_ext.ensure_schema(store)
    return store


def _approved_question_item(store):
    """A `question` call needs no booking write at all - the send step
    should still be blocked by shadow mode on the guest-facing send itself."""
    item = store.upsert_item("call", "review-test-1", kind="call",
                             payload={"transcript": "is there parking"})
    store.set_fields(item.id, intent="question",
                     draft={"subject": "Parking", "body": "No parking on site.",
                           "needs_human": False, "channel": "email",
                           "caller": {"name": "Test", "email": "test@example.com"},
                           "pending_booking": {"kind": "none"}})
    store.transition(item.id, "pending_review", "agent")
    approve(store, item.id)
    return item.id


def test_send_blocked_in_shadow_mode_leaves_the_approval_standing(tmp_path, capsys):
    store = _store(tmp_path)
    item_id = _approved_question_item(store)
    args = argparse.Namespace(limit=10)
    code = cmd_send(store, _settings("shadow"), args)
    assert code == 1
    item = store.get_item(item_id)
    assert item.review_status == "approved"   # not "sent" - the approval is kept
    out = capsys.readouterr().out
    assert "approval kept" in out


def test_send_with_no_callback_channel_fails_cleanly(tmp_path):
    store = _store(tmp_path, "review2.db")
    item = store.upsert_item("call", "review-test-2", kind="call",
                             payload={"transcript": "no contact info given"})
    store.set_fields(item.id, intent="question",
                     draft={"subject": "", "body": "hi", "needs_human": False,
                           "channel": "none", "caller": {}, "pending_booking": {"kind": "none"}})
    store.transition(item.id, "pending_review", "agent")
    approve(store, item.id)
    code = cmd_send(store, _settings("live"), argparse.Namespace(limit=10))
    assert code == 1
    assert store.get_item(item.id).review_status == "failed"


def test_send_nothing_waiting_is_a_clean_no_op(tmp_path, capsys):
    store = _store(tmp_path, "review3.db")
    code = cmd_send(store, _settings("shadow"), argparse.Namespace(limit=10))
    assert code == 0
    assert "Nothing approved" in capsys.readouterr().out
