"""Tests for core.store — the SQLite item store and its review_status FSM.

Shared across every repo (factory/scaffold/tests/). Each test gets its own
throwaway database via tmp_path, so nothing here touches data/agent.db.
"""

from __future__ import annotations

import pytest

from core.store import IllegalTransition, Store


@pytest.fixture()
def store(tmp_path):
    s = Store(path=tmp_path / "test.db")
    yield s
    s.close()


def test_upsert_item_is_idempotent_on_source_and_external_id(store):
    first = store.upsert_item("email", "msg-1", payload={"subject": "hi"})
    again = store.upsert_item("email", "msg-1", payload={"subject": "hi"})
    assert first.id == again.id
    assert first.review_status == "new"
    assert len(store.list_items()) == 1


def test_valid_transition_updates_status_and_records_event(store):
    item = store.upsert_item("email", "msg-2")
    updated = store.transition(item.id, "pending_review", actor="agent")
    assert updated.review_status == "pending_review"
    events = store.list_events(item.id)
    assert any(e["action"] == "status:pending_review" for e in events)


def test_illegal_transition_raises(store):
    item = store.upsert_item("email", "msg-3")
    with pytest.raises(IllegalTransition):
        store.transition(item.id, "sent")  # new -> sent skips the whole pipeline


def test_claim_for_send_is_single_winner(store):
    item = store.upsert_item("email", "msg-4")
    store.transition(item.id, "pending_review")
    store.transition(item.id, "approved")

    claimed_first = store.claim_for_send(limit=5)
    claimed_second = store.claim_for_send(limit=5)
    assert [i.id for i in claimed_first] == [item.id]
    assert claimed_second == []  # already claimed, second caller gets nothing

    sent = store.mark_sent(item.id, message_id="mock-123")
    assert sent.review_status == "sent"
    assert sent.sent_message_id == "mock-123"


def test_mark_send_failed_then_human_retry(store):
    item = store.upsert_item("email", "msg-5")
    store.transition(item.id, "pending_review")
    store.transition(item.id, "approved")
    store.claim_for_send()
    failed = store.mark_send_failed(item.id, "smtp timeout")
    assert failed.review_status == "failed"
    retried = store.transition(item.id, "approved", actor="human")
    assert retried.review_status == "approved"


def test_reap_stuck_sending_moves_old_rows_to_failed(store):
    item = store.upsert_item("email", "msg-6")
    store.transition(item.id, "pending_review")
    store.transition(item.id, "approved")
    store.claim_for_send()
    # backdate updated_at so the reaper considers it stuck
    store.db.execute("UPDATE items SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id=?",
                     (item.id,))
    reaped = store.reap_stuck_sending(max_age_minutes=30)
    assert reaped == [item.id]
    assert store.get_item(item.id).review_status == "failed"


def test_upsert_unique_dedups_on_kind_and_key(store):
    first, created1 = store.upsert_unique("digest", "2026-09-01")
    second, created2 = store.upsert_unique("digest", "2026-09-01")
    assert created1 is True
    assert created2 is False
    assert first.id == second.id


def test_next_sequence_dry_run_never_burns_a_number(store):
    peek1 = store.next_sequence("invoice", dry_run=True)
    peek2 = store.next_sequence("invoice", dry_run=True)
    assert peek1 == peek2 == 1
    real1 = store.next_sequence("invoice")
    real2 = store.next_sequence("invoice")
    assert real1 == 1
    assert real2 == 2


def test_migrate_runs_an_agent_schema_idempotently(store):
    sql = "CREATE TABLE IF NOT EXISTS agent_widgets (id TEXT PRIMARY KEY, n INTEGER);"
    store.migrate(sql)
    store.db.execute("INSERT INTO agent_widgets VALUES ('w1', 1)")
    store.migrate(sql)  # second run must not fail or wipe the row
    assert store.db.execute("SELECT n FROM agent_widgets WHERE id='w1'").fetchone()[0] == 1


def test_already_processed_ignores_items_still_new(store):
    parked = store.upsert_item("email", "m-parked", kind="email", payload={})
    done = store.upsert_item("email", "m-done", kind="email", payload={})
    store.transition(done.id, "pending_review", actor="agent")
    seen = store.already_processed("email", ["m-parked", "m-done", "m-unknown"])
    assert seen == {"m-done"}, "a row still in `new` was parked mid-pass and must be retried"
    assert parked.review_status == "new"


def test_upsert_payload_refresh_keeps_underscore_stage_caches(store):
    item = store.upsert_item("email", "m-cache", kind="email", payload={"subject": "hi"})
    store.set_fields(item.id, payload={"subject": "hi", "_stage_cache": {"intent": "question"}})
    refreshed = store.upsert_item("email", "m-cache", kind="email",
                                  payload={"subject": "hi", "body": "longer"})
    assert refreshed.payload["_stage_cache"] == {"intent": "question"}
    assert refreshed.payload["body"] == "longer"


def test_blocked_send_returns_to_approved_not_failed(store):
    item = store.upsert_item("email", "m-blk", kind="email", payload={})
    store.transition(item.id, "pending_review"); store.transition(item.id, "approved")
    claimed = store.claim_for_send(limit=5)
    assert [c.id for c in claimed] == [item.id]
    # a guard block is not a failure: the approval stands for go-live
    store.transition(item.id, "approved", "agent", {"blocked": "mode is shadow"})
    assert store.get_item(item.id).review_status == "approved"


def test_mock_adapter_items_are_tagged_sample_outside_demo(tmp_path, monkeypatch):
    from core.config import load_settings
    from core.adapters import sample_data_warning
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    real = load_settings(provider="mock", mode="shadow")       # adapters default to mock
    assert real.systems.email.adapter == "mock"
    assert "SAMPLE DATA" in (sample_data_warning(real) or "")
    s = Store(real, path=tmp_path / "s.db")
    assert s.upsert_item("email", "m1", kind="email", payload={"x": 1}).payload.get("_sample") is True
    demo = load_settings(demo=True)
    assert sample_data_warning(demo) is None
    d = Store(demo, path=tmp_path / "d.db")
    assert "_sample" not in d.upsert_item("email", "m1", kind="email", payload={"x": 1}).payload


def test_systems_used_narrows_the_sample_data_warning(tmp_path, monkeypatch):
    from core.config import load_settings
    from core.adapters import sample_data_warning
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    cfg = tmp_path / "config"; cfg.mkdir()
    (cfg / "agent.yaml").write_text("systems_used: [messaging]\n", encoding="utf-8")
    (cfg / "hotel.yaml").write_text("systems:\n  messaging:\n    adapter: webhook\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CONFIG_DIR", str(cfg))
    s = load_settings(provider="mock", mode="shadow")
    assert s.systems.pms.adapter == "mock" and sample_data_warning(s) is None


def test_empty_systems_used_means_no_sample_warning(tmp_path, monkeypatch):
    from core.config import load_settings
    from core.adapters import sample_data_warning
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    cfg = tmp_path / "config"; cfg.mkdir()
    (cfg / "agent.yaml").write_text("systems_used: []\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CONFIG_DIR", str(cfg))
    assert sample_data_warning(load_settings(provider="mock", mode="shadow")) is None
