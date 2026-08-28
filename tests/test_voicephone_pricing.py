"""Tests for tools/pricing.py - pure functions, no store, no I/O."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pytest

from pricing import (UnknownRoomType, is_large_group, is_large_party, is_service_closed,
                     is_within_service_hours, match_menu_item, match_room_type, nightly_rate,
                     nights_between, price_order, seating_window, stay_total)

ROOM_TYPES = {
    "classic": {"name": "Classic Room", "base_rate": 160, "max_occupancy": 2, "count": 10},
    "sea-view": {"name": "Sea View Room", "base_rate": 220, "max_occupancy": 2, "count": 5},
}

MENU = [
    {"id": "club-sandwich", "title": "Club Sandwich", "price": 28, "hints": ["club", "sandwich"]},
    {"id": "sparkling-water", "title": "Sparkling Water", "price": 6, "hints": ["water"]},
]


def test_match_room_type_exact_slug_and_display_name():
    assert match_room_type("classic", ROOM_TYPES) == "classic"
    assert match_room_type("Sea View Room", ROOM_TYPES) == "sea-view"


def test_match_room_type_fuzzy_and_hyphen_normalised():
    assert match_room_type("sea view", ROOM_TYPES) == "sea-view"
    assert match_room_type("a classic room please", ROOM_TYPES) == "classic"


def test_match_room_type_unknown_lists_options():
    with pytest.raises(UnknownRoomType) as exc:
        match_room_type("penthouse", ROOM_TYPES)
    assert "classic" in str(exc.value) and "sea-view" in str(exc.value)


def test_nightly_rate_weekend_and_season_multiplier():
    # January (season 0.85), a Tuesday: 160 * 0.85 = 136 -> rounds to 135
    weekday_rate = nightly_rate("classic", date(2027, 1, 5), ROOM_TYPES)
    assert weekday_rate == 135
    # A Friday in January: 160 * 0.85 * 1.08 = 146.88 -> rounds to 145
    weekend_rate = nightly_rate("classic", date(2027, 1, 8), ROOM_TYPES)
    assert weekend_rate == 145


def test_stay_total_sums_every_night():
    total = stay_total("classic", "2027-01-04", "2027-01-06", ROOM_TYPES)
    assert total == nightly_rate("classic", date(2027, 1, 4), ROOM_TYPES) \
        + nightly_rate("classic", date(2027, 1, 5), ROOM_TYPES)


def test_stay_total_rejects_checkout_before_checkin():
    with pytest.raises(ValueError):
        stay_total("classic", "2027-01-06", "2027-01-04", ROOM_TYPES)


def test_nights_between():
    assert nights_between("2026-09-12", "2026-09-14") == 2


def test_is_large_group_threshold():
    assert is_large_group(6, large_group_pax=6) is True
    assert is_large_group(5, large_group_pax=6) is False


def test_is_service_closed_matches_configured_weekday():
    monday = date(2026, 9, 14)
    assert is_service_closed(monday, [0]) is True
    tuesday = date(2026, 9, 15)
    assert is_service_closed(tuesday, [0]) is False


def test_is_within_service_hours():
    assert is_within_service_hours("20:00", "19:00", "22:30") is True
    assert is_within_service_hours("18:00", "19:00", "22:30") is False
    assert is_within_service_hours("22:30", "19:00", "22:30") is True


def test_is_large_party_threshold():
    assert is_large_party(7, large_party_size=7) is True
    assert is_large_party(6, large_party_size=7) is False


def test_seating_window_clamped_to_the_day():
    start, end = seating_window("21:00", window_minutes=90)
    assert start == "19:30"
    assert end == "22:30"
    # A very early time clamps at 00:00 rather than wrapping into the day before.
    start, end = seating_window("00:30", window_minutes=90)
    assert start == "00:00"


def test_match_menu_item_exact_and_hint():
    assert match_menu_item("Club Sandwich", MENU)["id"] == "club-sandwich"
    assert match_menu_item("could I get some water", MENU)["id"] == "sparkling-water"
    assert match_menu_item("chicken wings", MENU) is None


def test_price_order_applies_quantities_and_one_tray_charge():
    order = price_order([{"name": "club sandwich", "qty": 2}, {"name": "sparkling water"}],
                        MENU, tray_charge=8)
    assert order.items_total == 28 * 2 + 6 * 1
    assert order.tray_charge == 8
    assert order.grand_total == 28 * 2 + 6 + 8
    assert order.all_matched is True


def test_price_order_reports_unmatched_items_by_name():
    order = price_order([{"name": "club sandwich"}, {"name": "chicken wings", "qty": 3}],
                        MENU, tray_charge=8)
    assert order.unmatched == ["chicken wings"]
    assert order.all_matched is False
    assert order.items_total == 28
