"""Tests for core.review — the write guard and the review queue helpers.

Shared across every repo (factory/scaffold/tests/). Builds Settings/Item
objects directly (no yaml files, no network) so it is fast and portable.
"""

from __future__ import annotations

from core.config import ReviewConfig, Settings
from core.review import WriteBlocked, assert_write_allowed, edit, evaluate_write, resolve_autonomy
from core.store import Store


def _settings(mode: str = "shadow", dry_run: bool = False,
             require_approval_for=None) -> Settings:
    review = ReviewConfig(require_approval_for=require_approval_for
                          if require_approval_for is not None
                          else ["send_email", "send_message"])
    return Settings(mode=mode, dry_run=dry_run, review=review)


def test_shadow_mode_blocks_writes_with_no_item():
    decision = evaluate_write(_settings(mode="shadow"), "send_email", item=None)
    assert decision.allowed is False
    assert "shadow" in decision.reason


def test_shadow_mode_blocks_even_an_approved_item(tmp_path):
    store = Store(path=tmp_path / "g.db")
    item = store.upsert_item("email", "m1", kind="email", payload={})
    store.transition(item.id, "pending_review")
    approved = store.transition(item.id, "approved")
    decision = evaluate_write(_settings(mode="shadow"), "send_email", item=approved)
    assert decision.allowed is False, "nothing leaves in shadow mode, approved or not"
    assert "recorded" in decision.reason


def test_stale_backlog_clears_shadow_era_approvals(tmp_path):
    from core.review import stale_backlog
    store = Store(path=tmp_path / "g2.db")
    ids = []
    for n, status in enumerate(("approved", "edited", "pending_review")):
        it = store.upsert_item("email", f"s{n}", kind="email", payload={})
        store.transition(it.id, "pending_review")
        if status != "pending_review":
            store.transition(it.id, status)
        ids.append(it.id)
    sent = store.upsert_item("email", "sent1", kind="email", payload={})
    store.transition(sent.id, "pending_review"); store.transition(sent.id, "approved")
    store.transition(sent.id, "sending"); store.transition(sent.id, "sent")
    moved = stale_backlog(store)
    assert set(moved) == set(ids)
    assert store.get_item(sent.id).review_status == "sent"


def test_dry_run_blocks_unconditionally_even_when_approved(tmp_path):
    store = Store(path=tmp_path / "t2.db")
    item = store.upsert_item("email", "e2")
    store.transition(item.id, "pending_review")
    approved = store.transition(item.id, "approved")
    decision = evaluate_write(_settings(mode="live", dry_run=True), "send_email",
                              item=approved)
    assert decision.allowed is False
    store.close()


def test_live_mode_needs_approval_for_gated_actions():
    settings = _settings(mode="live")
    decision = evaluate_write(settings, "send_email", item=None)
    assert decision.allowed is False
    assert "needs approval" in decision.reason


def test_live_mode_allows_ungated_actions_straight_through():
    settings = _settings(mode="live", require_approval_for=["payment"])
    decision = evaluate_write(settings, "send_email", item=None)
    assert decision.allowed is True


def test_already_sent_item_is_never_written_twice(tmp_path):
    store = Store(path=tmp_path / "t3.db")
    item = store.upsert_item("email", "e3")
    store.transition(item.id, "pending_review")
    store.transition(item.id, "approved")
    store.claim_for_send()
    sent = store.mark_sent(item.id, "msg-1")
    decision = evaluate_write(_settings(mode="live"), "send_email", item=sent)
    assert decision.allowed is False
    assert "already" in decision.reason
    store.close()


def test_assert_write_allowed_raises_writeblocked():
    try:
        assert_write_allowed(_settings(mode="shadow"), "send_email", item=None)
    except WriteBlocked as exc:
        assert "send_email" in str(exc)
    else:
        raise AssertionError("expected WriteBlocked")


def test_resolve_autonomy_shadow_always_drafts():
    settings = _settings(mode="shadow")
    assert resolve_autonomy(settings, agent_default="send") == "draft"


def test_resolve_autonomy_live_with_gate_always_drafts():
    settings = _settings(mode="live")
    assert resolve_autonomy(settings, agent_default="send", gates=["low_confidence"]) == "draft"


def test_resolve_autonomy_live_send_when_nothing_stops_it():
    settings = _settings(mode="live")
    assert resolve_autonomy(settings, agent_default="send") == "send"


def test_edit_records_a_learning_for_the_coach(tmp_path):
    store = Store(path=tmp_path / "t4.db")
    item = store.upsert_item("email", "e4")
    store.set_fields(item.id, draft={"body": "original text"})
    store.transition(item.id, "pending_review")
    edited = edit(store, item.id, {"body": "improved text"}, note="tone")
    assert edited.review_status == "edited"
    learnings = store.list_learnings()
    assert any(l["before"] == "original text" and l["after"] == "improved text"
              for l in learnings)
    store.close()


def test_money_actions_always_need_a_human_even_when_config_ungates_them(tmp_path):
    from core.review import evaluate_write
    settings = _settings(mode="live")
    settings.review.require_approval_for = []          # a hotel removed every gate
    assert evaluate_write(settings, "send_email", item=None).allowed is True
    decision = evaluate_write(settings, "payment", item=None)
    assert decision.allowed is False and "never moves unattended" in decision.reason
    store = Store(path=tmp_path / "money.db")
    item = store.upsert_item("invoice", "inv-1", kind="payment", payload={})
    store.transition(item.id, "pending_review"); approved = store.transition(item.id, "approved")
    assert evaluate_write(settings, "payment", item=approved).allowed is True
