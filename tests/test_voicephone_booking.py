"""Tests for tools/booking.py - the preview/finalize split (design decision 3)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.adapters import get_pms
from core.config import load_settings
from core.review import WriteBlocked
from core.store import Store

import store_ext
from booking import (finalize_action, preview_guest_request, preview_room,
                     preview_room_service, preview_table)


def _settings(mode="shadow"):
    return load_settings(provider="mock", mode=mode)


def _store(tmp_path, name="booking.db"):
    store = Store(_settings(), path=tmp_path / name)
    store_ext.ensure_schema(store)
    return store


def test_preview_room_computes_real_availability_from_the_pms(tmp_path):
    """fixtures/hotel/reservations.json books the Hotel Aurora Suite solid
    for 12-14 September (three confirmed reservations against a count of 3
    in config/agent.example.yaml) - so a fresh request for the same dates
    must come back sold out, not guessed."""
    settings = _settings()
    store = _store(tmp_path)
    pms = get_pms(settings)
    outcome = preview_room(settings, pms, store, {"room_type": "house-suite",
                                                  "checkin": "2026-09-12",
                                                  "checkout": "2026-09-14", "guests": 2})
    assert outcome.ok is False
    assert outcome.needs_human is False
    assert "fully booked" in outcome.error.lower()


def test_preview_room_finds_a_free_type_and_prices_it(tmp_path):
    settings = _settings()
    store = _store(tmp_path)
    pms = get_pms(settings)
    outcome = preview_room(settings, pms, store, {"room_type": "sea-view",
                                                  "checkin": "2026-09-19",
                                                  "checkout": "2026-09-21", "guests": 2})
    assert outcome.ok is True
    assert outcome.needs_human is False
    assert outcome.total > 0
    assert outcome.params["room_type"] == "sea-view"


def test_preview_room_counts_this_agents_own_already_finalized_bookings(tmp_path):
    """Once this agent has itself finalized a room booking (see
    test_finalize_action_writes_room_booking below), a later preview for the
    same overlapping dates must count it - not just what the real PMS
    already knew about. classic has count=20 in the shipped config, so seed
    20 of this agent's own confirmed bookings to sell it out completely."""
    settings = _settings()
    store = _store(tmp_path)
    pms = get_pms(settings)
    for i in range(20):
        store.db.execute(
            "INSERT INTO room_bookings (id, ref, item_id, caller_name, room_type, checkin, "
            "checkout, guests, total, channel, status, notes, created_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"seed-{i}", f"RES-{i}", None, "Seed", "classic", "2026-11-01", "2026-11-03",
             2, 300, "voice", "confirmed", "", "2026-01-01T00:00:00"))
    outcome = preview_room(settings, pms, store, {"room_type": "classic",
                                                  "checkin": "2026-11-01",
                                                  "checkout": "2026-11-03", "guests": 2})
    assert outcome.ok is False
    assert "fully booked" in outcome.error.lower()


def test_preview_room_missing_dates_needs_a_human(tmp_path):
    settings = _settings()
    store = _store(tmp_path)
    pms = get_pms(settings)
    outcome = preview_room(settings, pms, store, {"room_type": "classic"})
    assert outcome.ok is False
    assert outcome.needs_human is True


def test_preview_room_large_group_flagged(tmp_path):
    settings = _settings()
    store = _store(tmp_path)
    pms = get_pms(settings)
    outcome = preview_room(settings, pms, store, {"room_type": "classic",
                                                  "checkin": "2026-10-02",
                                                  "checkout": "2026-10-05", "guests": 7})
    assert outcome.ok is True
    assert outcome.needs_human is True


def test_preview_table_offers_no_human_when_the_day_is_simply_closed(tmp_path):
    settings = _settings()
    store = _store(tmp_path)
    outcome = preview_table(settings, store, {"date": "2026-09-14", "time": "19:30",
                                              "party_size": 4})
    assert outcome.ok is False
    assert outcome.needs_human is False
    assert "closed" in outcome.error.lower()


def test_preview_table_large_party_always_needs_a_human(tmp_path):
    settings = _settings()
    store = _store(tmp_path)
    outcome = preview_table(settings, store, {"date": "2026-09-15", "time": "20:00",
                                              "party_size": 8})
    assert outcome.ok is True
    assert outcome.needs_human is True


def test_preview_table_respects_the_cover_cap(tmp_path):
    """restaurant.cover_cap is 40 in the shipped config; seat 38 covers at
    19:30 and confirm a party of 4 at 20:00 (within the seating window)
    cannot fit, while a normal-sized table elsewhere still can."""
    settings = _settings()
    store = _store(tmp_path)
    store.db.execute(
        "INSERT INTO table_bookings (id, ref, item_id, caller_name, party_size, date, time, "
        "dietary_notes, special_requests, channel, status, created_at) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("seed-1", "TBL-0001", None, "Seed", 38, "2026-09-15", "19:30", None, None, "voice",
         "confirmed", "2026-01-01T00:00:00"))
    outcome = preview_table(settings, store, {"date": "2026-09-15", "time": "20:00",
                                              "party_size": 4})
    assert outcome.ok is False
    assert outcome.needs_human is False
    assert "committed" in outcome.error.lower()


def test_preview_room_service_matches_menu_and_prices_it(tmp_path):
    settings = _settings()
    outcome = preview_room_service(settings, {"room_number": "214",
                                              "items": [{"name": "Club Sandwich", "qty": 1},
                                                       {"name": "Sparkling Water", "qty": 2}]})
    assert outcome.ok is True
    assert outcome.needs_human is False
    assert outcome.total == 28 + 6 * 2 + 8


def test_preview_room_service_unmatched_item_needs_a_human(tmp_path):
    settings = _settings()
    outcome = preview_room_service(settings, {"room_number": "118",
                                              "items": [{"name": "chicken wings"}]})
    assert outcome.ok is False
    assert outcome.needs_human is True


def test_preview_guest_request_flags_urgent_category(tmp_path):
    settings = _settings()
    outcome = preview_guest_request(settings, {"category": "transport",
                                               "details": "Airport transfer at 21:40"})
    assert outcome.ok is True
    assert outcome.params["urgent"] is True


def test_preview_guest_request_missing_details_needs_a_human(tmp_path):
    settings = _settings()
    outcome = preview_guest_request(settings, {"category": "housekeeping", "details": ""})
    assert outcome.ok is False
    assert outcome.needs_human is True


def _sending_item(store, *, pending: dict, channel: str = "email",
                  caller: dict | None = None, reservation_ref: str | None = None):
    """Build an item all the way to `sending`, the state `finalize_action`
    is always called on in the real flow (`tools/review.py send`)."""
    item = store.upsert_item("call", pending["kind"] + "-test", kind="call",
                             payload={"transcript": "test"})
    store.set_fields(item.id, draft={"subject": "", "body": "hi", "needs_human": False,
                                     "channel": channel, "caller": caller or {},
                                     "reservation_ref": reservation_ref,
                                     "pending_booking": pending})
    store.transition(item.id, "pending_review", "agent")
    store.transition(item.id, "approved", "human")
    return store.claim_for_send(limit=1)[0]


def test_finalize_action_blocked_in_shadow_mode(tmp_path):
    store = _store(tmp_path, "finalize.db")
    pending = {"kind": "room", "ok": True, "needs_human": False, "total": 300,
              "detail": "Classic Room", "params": {"room_type": "classic",
                                                    "checkin": "2027-01-04",
                                                    "checkout": "2027-01-06", "guests": 2}}
    item = _sending_item(store, pending=pending)
    import pytest
    with pytest.raises(WriteBlocked):
        finalize_action(_settings("shadow"), store, item)
    # No row was written.
    n = store.db.execute("SELECT COUNT(*) AS n FROM room_bookings").fetchone()["n"]
    assert n == 0


def test_finalize_action_writes_room_booking_in_live_mode(tmp_path):
    store = _store(tmp_path, "finalize2.db")
    pending = {"kind": "room", "ok": True, "needs_human": False, "total": 300,
              "detail": "Classic Room", "params": {"room_type": "classic",
                                                    "checkin": "2027-01-04",
                                                    "checkout": "2027-01-06", "guests": 2}}
    item = _sending_item(store, pending=pending, caller={"name": "Test Caller"})
    outcome = finalize_action(_settings("live"), store, item)
    assert outcome.ok is True
    assert outcome.ref.startswith("RES-")
    row = store.db.execute("SELECT * FROM room_bookings WHERE ref=?", (outcome.ref,)).fetchone()
    assert row["room_type"] == "classic"
    assert row["status"] == "confirmed"


def test_finalize_action_never_writes_a_row_the_preview_already_rejected(tmp_path):
    """A closed-day table or a sold-out room must never become a confirmed
    row, approved or not - see docs/how-it-works.md design decision 3."""
    store = _store(tmp_path, "finalize3.db")
    pending = {"kind": "table", "ok": False, "needs_human": False,
              "error": "Salt is closed on Mondays.", "params": {}}
    item = _sending_item(store, pending=pending)
    outcome = finalize_action(_settings("live"), store, item)
    assert outcome.ok is False
    n = store.db.execute("SELECT COUNT(*) AS n FROM table_bookings").fetchone()["n"]
    assert n == 0
