"""tools/store_ext.py - Voice / Phone AI's own tables, layered on core.store.Store.

The generic `items` table (core/store.py) is the review queue: one row per
call transcript waiting on a human or a send. It is not a booking ledger.
This module adds the tables this agent actually needs to query - room,
table and room-service bookings, and the guest-request (concierge) log - and
the overlap queries `tools/booking.py` uses for a genuine availability
check. See docs/how-it-works.md design decisions 2 and 6.

Call :func:`ensure_schema` once per `Store` before touching any of these
tables; every tool in this repo does it right after constructing its `Store`.
Nothing here replaces `core.store` - it is additive, using the same
connection (`store.db`) and the same `utcnow()` timestamp convention.
"""

from __future__ import annotations

import uuid
from typing import Any

from core.store import Store, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS room_bookings (
  id            TEXT PRIMARY KEY,
  ref           TEXT NOT NULL UNIQUE,
  item_id       TEXT,
  caller_name   TEXT,
  room_type     TEXT NOT NULL,
  checkin       TEXT NOT NULL,
  checkout      TEXT NOT NULL,
  guests        INTEGER NOT NULL DEFAULT 2,
  total         REAL NOT NULL,
  channel       TEXT,
  status        TEXT NOT NULL DEFAULT 'confirmed',
  notes         TEXT,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_room_bookings_dates ON room_bookings (room_type, checkin, checkout, status);

CREATE TABLE IF NOT EXISTS table_bookings (
  id               TEXT PRIMARY KEY,
  ref              TEXT NOT NULL UNIQUE,
  item_id          TEXT,
  caller_name      TEXT,
  party_size       INTEGER NOT NULL DEFAULT 2,
  date             TEXT NOT NULL,
  time             TEXT NOT NULL,
  dietary_notes    TEXT,
  special_requests TEXT,
  channel          TEXT,
  status           TEXT NOT NULL DEFAULT 'confirmed',
  created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_table_bookings_date ON table_bookings (date, status);

CREATE TABLE IF NOT EXISTS room_service_orders (
  id                TEXT PRIMARY KEY,
  ref               TEXT NOT NULL UNIQUE,
  item_id           TEXT,
  caller_name       TEXT,
  room_number       TEXT,
  items_json        TEXT NOT NULL,
  items_total       REAL NOT NULL,
  tray_charge       REAL NOT NULL,
  total             REAL NOT NULL,
  estimated_delivery_minutes INTEGER,
  notes             TEXT,
  channel           TEXT,
  status            TEXT NOT NULL DEFAULT 'placed',
  created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guest_requests (
  id            TEXT PRIMARY KEY,
  ref           TEXT NOT NULL UNIQUE,
  item_id       TEXT,
  caller_name   TEXT,
  room_number   TEXT,
  category      TEXT NOT NULL,
  details       TEXT NOT NULL,
  urgent        INTEGER NOT NULL DEFAULT 0,
  channel       TEXT,
  status        TEXT NOT NULL DEFAULT 'open',
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_guest_requests_status ON guest_requests (status, created_at);
"""


def ensure_schema(store: Store) -> None:
    """Create every table above if it does not already exist. Idempotent."""
    store.db.executescript(SCHEMA)


def new_id() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------
# genuine availability - see docs/how-it-works.md design decision 6
# --------------------------------------------------------------------------
def count_overlapping_room_bookings(store: Store, room_type: str, checkin: str,
                                    checkout: str) -> int:
    """Confirmed bookings this agent has itself finalized for ``room_type``
    whose stay overlaps ``[checkin, checkout)`` - added to the overlap count
    from the real PMS (``tools/booking.py:_pms_overlap_count``) so a later
    call cannot be quoted a room a human already approved and sent for the
    same dates, even before that booking has reached the real PMS. A
    preview never writes (see docs/how-it-works.md design decision 3), so
    two calls previewed in the *same* pass, before either is approved,
    still see the same starting availability - this only closes the gap
    once the first one has actually been finalized."""
    row = store.db.execute(
        "SELECT COUNT(*) AS n FROM room_bookings WHERE room_type=? AND status='confirmed' "
        "AND checkin < ? AND checkout > ?", (room_type, checkout, checkin)).fetchone()
    return int(row["n"] if row else 0)


def count_table_covers_in_window(store: Store, date_: str, window_start: str,
                                 window_end: str) -> int:
    """Sum of ``party_size`` for this agent's own confirmed table bookings on
    ``date_`` whose time falls in ``[window_start, window_end]`` (``HH:MM``
    strings, both inclusive) - see ``tools/pricing.py:seating_window``."""
    rows = store.db.execute(
        "SELECT party_size FROM table_bookings WHERE date=? AND status='confirmed' "
        "AND time >= ? AND time <= ?", (date_, window_start, window_end)).fetchall()
    return sum(int(r["party_size"]) for r in rows)


# --------------------------------------------------------------------------
# reads for tools/report.py and workflows/80-review.md
# --------------------------------------------------------------------------
def counts_by_kind(store: Store, table: str) -> dict[str, int]:
    rows = store.db.execute(f"SELECT status, COUNT(*) AS n FROM {table} GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def open_guest_requests(store: Store, limit: int = 50) -> list[dict]:
    rows = store.db.execute(
        "SELECT * FROM guest_requests WHERE status='open' ORDER BY created_at ASC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]
