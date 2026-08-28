"""tools/booking.py - the four actions Voice / Phone AI can take on a call.

Two phases, on purpose (see docs/how-it-works.md, design decision 3):

**Preview** (`compute_pending`) runs during classify, for every call. It
validates the caller's request against the property's own data - room
availability, restaurant hours, the room-service menu - and returns what
*would* happen. It never writes anything. This is what lets the draft
callback describe a booking honestly without a person having approved it
yet.

**Finalize** (`finalize_action`) runs once, at send time, only for an item a
human approved or edited (`tools/review.py send`). It is the only function
in this module that touches `store_ext`'s tables or calls an adapter write.
It re-checks `core.review.assert_write_allowed` itself, so even a future
caller that skips the review queue cannot make it write in shadow mode.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field

from core.adapters import get_messaging, get_pms
from core.adapters.base import AdapterError
from core.review import WriteBlocked, assert_write_allowed
from core.store import Item, Store, utcnow

import store_ext
from pricing import (UnknownRoomType, is_large_group, is_large_party, is_service_closed,
                     is_within_service_hours, match_room_type, nights_between, parse_date,
                     price_order, seating_window, stay_total)


@dataclass
class BookingOutcome:
    """What a preview decided, whether or not it has been written yet."""

    ok: bool
    kind: str                       # room | table | room_service | guest_request | none
    ref: str = ""
    detail: str = ""
    error: str = ""
    needs_human: bool = False
    total: float = 0.0
    params: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "kind": self.kind, "ref": self.ref, "detail": self.detail,
               "error": self.error, "needs_human": self.needs_human,
               "total": self.total, "params": self.params}


def _ref(prefix: str) -> str:
    return f"{prefix}-{random.randint(1000, 9999)}"


# --------------------------------------------------------------------------
# preview - read-only, runs for every call regardless of mode
# --------------------------------------------------------------------------
def _pms_overlap_count(pms, room_type: str, checkin: str, checkout: str) -> int:
    """Confirmed reservations already in the real PMS that overlap the stay -
    the other half of the genuine availability check alongside
    ``store_ext.count_overlapping_room_bookings``. A PMS that cannot answer
    (misconfigured, unreachable) counts as zero rather than blocking the
    preview - the mock/csv/cloudbeds adapters are all reads here, and a
    read failure should degrade to "we don't know of a conflict", not crash
    the call."""
    try:
        reservations = pms.list_reservations(checkin, checkout, status="confirmed")
    except (AdapterError, NotImplementedError):
        return 0
    return sum(1 for r in reservations
              if r.room_type_id == room_type and r.check_in < checkout and r.check_out > checkin)


def preview_room(settings, pms, store: Store, booking: dict) -> BookingOutcome:
    room_types = settings.agent_get("rooms.room_types", {})
    checkin, checkout = booking.get("checkin"), booking.get("checkout")
    guests = int(booking.get("guests") or 2)
    if not (checkin and checkout):
        return BookingOutcome(False, "room", error="missing check-in or check-out date",
                              needs_human=True, params=booking)

    room_type = booking.get("room_type")
    if room_type:
        try:
            room_type = match_room_type(room_type, room_types)
        except UnknownRoomType as exc:
            return BookingOutcome(False, "room", error=str(exc), needs_human=True, params=booking)
    candidates = [room_type] if room_type else list(room_types)

    try:
        nights = nights_between(checkin, checkout)
        if nights <= 0:
            raise ValueError("checkout must be after checkin")
    except ValueError as exc:
        return BookingOutcome(False, "room", error=str(exc), needs_human=True, params=booking)

    season = settings.agent_get("rooms.season_multiplier")
    weekend_multiplier = float(settings.agent_get("rooms.weekend_multiplier", 1.08))
    weekend_days = tuple(settings.agent_get("rooms.weekend_days", [4, 5]))
    large_group_pax = int(settings.agent_get("rooms.large_group_pax", 6))

    options = []
    for slug in candidates:
        cfg = room_types.get(slug, {})
        cap = int(cfg.get("count", 0))
        booked = (_pms_overlap_count(pms, slug, checkin, checkout)
                 + store_ext.count_overlapping_room_bookings(store, slug, checkin, checkout))
        available = max(0, cap - booked)
        if available <= 0:
            continue
        total = stay_total(slug, checkin, checkout, room_types, season=season,
                           weekend_multiplier=weekend_multiplier, weekend_days=weekend_days)
        options.append({"room_type": slug, "name": cfg.get("name", slug), "available": available,
                        "total": total, "nights": nights})

    if not options:
        return BookingOutcome(False, "room", needs_human=False,
                              error="Fully booked for those dates - suggest shifting the "
                                    "stay by a day or two.", params=booking)

    currency = settings.hotel.currency
    if room_type:
        opt = options[0]
        large = is_large_group(guests, large_group_pax)
        detail = (f"{opt['name']} - {checkin} to {checkout} ({nights} night(s)) - "
                 f"{guests} guest(s) - {currency} {opt['total']:.0f} - {opt['available']} left")
        return BookingOutcome(True, "room", detail=detail, needs_human=large, total=opt["total"],
                              params={**booking, "room_type": room_type, "guests": guests,
                                     "nights": nights})

    # No room type named - list what's available and let a human confirm which one.
    listing = "; ".join(f"{o['name']} ({currency} {o['total']:.0f}, {o['available']} left)"
                        for o in options[:4])
    return BookingOutcome(True, "room", needs_human=True, detail=f"Options: {listing}",
                          params={**booking, "guests": guests, "nights": nights,
                                 "options": options})


def preview_table(settings, store: Store, booking: dict) -> BookingOutcome:
    restaurant = settings.agent_get("restaurant", {})
    name = restaurant.get("name", "the restaurant")
    date_ = booking.get("date")
    time_ = booking.get("time") or restaurant.get("default_time", "19:30")
    party_size = int(booking.get("party_size") or 2)
    if not date_:
        return BookingOutcome(False, "table", error="missing date", needs_human=True,
                              params=booking)
    try:
        day = parse_date(date_)
    except ValueError:
        return BookingOutcome(False, "table", error=f"'{date_}' is not a valid date",
                              needs_human=True, params=booking)
    try:
        service_hours_ok = is_within_service_hours(
            time_, restaurant.get("service_start", "19:00"), restaurant.get("service_end", "22:30"))
    except ValueError:
        return BookingOutcome(False, "table", error=f"'{time_}' is not a valid time",
                              needs_human=True, params=booking)

    if is_service_closed(day, restaurant.get("closed_weekdays", [])):
        return BookingOutcome(
            False, "table", needs_human=False,
            error=f"{name} is closed on {day.strftime('%A')}s - offer the nearest other "
                 "evening instead.", params=booking)
    if not service_hours_ok:
        return BookingOutcome(
            False, "table", needs_human=False,
            error=f"Dinner service runs {restaurant.get('service_start', '19:00')}-"
                 f"{restaurant.get('service_end', '22:30')}.", params=booking)

    large_party_size = int(settings.agent_get("restaurant.large_party_size", 7))
    if is_large_party(party_size, large_party_size):
        detail = (f"{name} - {date_} {time_} - {party_size} guest(s) - party of "
                 f"{large_party_size}+, the restaurant team will call to confirm")
        return BookingOutcome(True, "table", needs_human=True, detail=detail,
                              params={**booking, "time": time_, "party_size": party_size})

    window_minutes = int(settings.agent_get("restaurant.seating_window_minutes", 90))
    window_start, window_end = seating_window(time_, window_minutes)
    seated = store_ext.count_table_covers_in_window(store, date_, window_start, window_end)
    cover_cap = int(settings.agent_get("restaurant.cover_cap", 40))
    remaining = cover_cap - seated
    if remaining < party_size:
        return BookingOutcome(
            False, "table", needs_human=False,
            error="That sitting is fully committed - offer 19:00 or 21:30 the same "
                 "evening, or the next day.", params=booking)

    detail = f"{name} - {date_} {time_} - {party_size} guest(s)"
    return BookingOutcome(True, "table", detail=detail,
                          params={**booking, "time": time_, "party_size": party_size})


def preview_room_service(settings, booking: dict) -> BookingOutcome:
    rs = settings.agent_get("room_service", {})
    menu = rs.get("menu", [])
    items = booking.get("items") or []
    room_number = booking.get("room_number")
    if not items:
        return BookingOutcome(False, "room_service", error="no items given", needs_human=True,
                              params=booking)
    if not room_number:
        return BookingOutcome(True, "room_service", needs_human=True,
                              error="missing room number", params=booking)

    order = price_order(items, menu, rs.get("tray_charge", 8))
    currency = settings.hotel.currency
    if not order.matched:
        return BookingOutcome(False, "room_service",
                              error=f"none of these are on the menu: {', '.join(order.unmatched)}",
                              needs_human=True, params=booking)

    lines = ", ".join(f"{m.qty}x {m.title}" for m in order.matched)
    detail = (f"Room {room_number} - {lines} - {currency} {order.items_total:.0f} + "
             f"{currency} {order.tray_charge:.0f} tray charge = {currency} {order.grand_total:.0f}")
    needs_human = bool(order.unmatched)
    if order.unmatched:
        detail += f" - could not match: {', '.join(order.unmatched)}"
    return BookingOutcome(True, "room_service", detail=detail, needs_human=needs_human,
                          total=order.grand_total,
                          params={**booking, "matched": [m.__dict__ for m in order.matched],
                                 "unmatched": order.unmatched, "items_total": order.items_total,
                                 "tray_charge": order.tray_charge,
                                 "estimated_delivery_minutes": rs.get("estimated_delivery_minutes", 35)})


def preview_guest_request(settings, booking: dict) -> BookingOutcome:
    category = booking.get("category") or "other"
    details = (booking.get("details") or "").strip()
    if not details:
        return BookingOutcome(False, "guest_request", error="missing what the guest actually "
                              "needs", needs_human=True, params=booking)
    urgent_categories = set(settings.agent_get("guest_requests.urgent_categories",
                                              ["transport", "safety"]))
    detail = f"{category}: {details}"
    return BookingOutcome(True, "guest_request", detail=detail,
                          params={**booking, "category": category, "details": details,
                                 "urgent": category in urgent_categories})


def compute_pending(settings, pms, store: Store, classification: dict) -> BookingOutcome:
    """Dispatch on the classify call_type to the matching preview function.

    Returns ``BookingOutcome(kind="none")`` for a call_type with nothing to
    book (a plain question) - the draft step still runs, it just has no
    booking result to reference.
    """
    call_type = classification.get("call_type")
    booking = classification.get("booking") or {}
    if call_type == "room_booking":
        return preview_room(settings, pms, store, booking)
    if call_type == "table_booking":
        return preview_table(settings, store, booking)
    if call_type == "room_service":
        return preview_room_service(settings, booking)
    if call_type == "guest_request":
        return preview_guest_request(settings, classification.get("guest_request") or {})
    return BookingOutcome(True, "none")


# --------------------------------------------------------------------------
# finalize - the only place that writes. Runs once, at send time.
# --------------------------------------------------------------------------
def finalize_action(settings, store: Store, item: Item) -> BookingOutcome:
    """Actually write the room/table/room-service/guest-request row (and any
    PMS note or staff notification) for an item a human has approved or
    edited. Called once from ``tools/review.py send``, on an item already
    flipped to ``sending`` by :meth:`core.store.Store.claim_for_send`.

    Re-checks the write guard itself (``pms_write``) so this function can
    never write in shadow mode or on ``--dry-run``, no matter who calls it.
    """
    pending = (item.draft or {}).get("pending_booking") or {"kind": "none"}
    kind = pending.get("kind", "none")
    if kind == "none":
        return BookingOutcome(True, "none")

    if not pending.get("ok", False):
        # The preview computed at classify time already rejected this
        # request (closed day, sold out, an unknown room type) - the
        # caller-facing callback that was approved and sent already says
        # so. Writing a "confirmed" row here anyway would contradict that
        # callback with a phantom booking nobody would think to look for.
        # See docs/how-it-works.md design decision 3.
        outcome = BookingOutcome(False, kind, error=pending.get("error") or
                                 "the preview did not succeed at classify time; nothing "
                                 "was written", params=pending.get("params") or {})
        store.record_event(item.id, "agent", "booking_not_finalized",
                           {"kind": kind, "reason": outcome.error})
        return outcome

    assert_write_allowed(settings, "pms_write", item)
    draft = item.draft or {}
    channel = draft.get("channel", "")
    caller = draft.get("caller") or {}
    caller_name = caller.get("name") or "Caller"
    params = pending.get("params") or {}
    now = utcnow()

    if kind == "room":
        ref = _ref("RES")
        store.db.execute(
            "INSERT INTO room_bookings (id, ref, item_id, caller_name, room_type, checkin, "
            "checkout, guests, total, channel, status, notes, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (store_ext.new_id(), ref, item.id, caller_name, params.get("room_type"),
             params.get("checkin"), params.get("checkout"), int(params.get("guests", 2)),
             float(pending.get("total", 0.0)), channel, "confirmed",
             "Captured by Voice / Phone AI from a call transcript", now))
        outcome = BookingOutcome(True, "room", ref=ref, detail=pending.get("detail", ""),
                                 total=pending.get("total", 0.0), params=params)
    elif kind == "table":
        ref = _ref("TBL")
        store.db.execute(
            "INSERT INTO table_bookings (id, ref, item_id, caller_name, party_size, date, "
            "time, dietary_notes, special_requests, channel, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (store_ext.new_id(), ref, item.id, caller_name, int(params.get("party_size", 2)),
             params.get("date"), params.get("time"), params.get("dietary_notes"),
             params.get("special_requests"), channel, "confirmed", now))
        outcome = BookingOutcome(True, "table", ref=ref, detail=pending.get("detail", ""),
                                 params=params)
    elif kind == "room_service":
        ref = _ref("RS")
        rs = settings.agent_get("room_service", {})
        store.db.execute(
            "INSERT INTO room_service_orders (id, ref, item_id, caller_name, room_number, "
            "items_json, items_total, tray_charge, total, estimated_delivery_minutes, notes, "
            "channel, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (store_ext.new_id(), ref, item.id, caller_name, params.get("room_number"),
             _json_list(params.get("matched", [])), float(params.get("items_total", 0.0)),
             float(params.get("tray_charge", 0.0)), float(pending.get("total", 0.0)),
             int(params.get("estimated_delivery_minutes", rs.get("estimated_delivery_minutes", 35))),
             params.get("notes"), channel, "placed", now))
        outcome = BookingOutcome(True, "room_service", ref=ref, detail=pending.get("detail", ""),
                                 total=pending.get("total", 0.0), params=params)
    elif kind == "guest_request":
        ref = _ref("REQ")
        urgent = bool(params.get("urgent"))
        store.db.execute(
            "INSERT INTO guest_requests (id, ref, item_id, caller_name, room_number, category, "
            "details, urgent, channel, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (store_ext.new_id(), ref, item.id, caller_name, params.get("room_number"),
             params.get("category"), params.get("details"), int(urgent), channel, "open", now))
        if urgent:
            _notify_staff(settings, f"Urgent guest request ({params.get('category')}) from "
                          f"{caller_name}: {params.get('details')} - ref {ref}", item)
        outcome = BookingOutcome(True, "guest_request", ref=ref, detail=pending.get("detail", ""),
                                 params=params)
    else:
        outcome = BookingOutcome(True, "none")

    reservation_ref = draft.get("reservation_ref")
    if reservation_ref:
        _append_pms_note(settings, reservation_ref,
                         f"{kind}: {pending.get('detail', '')} (ref {outcome.ref})", item)

    store.record_event(item.id, "agent", "booking_finalized", {"kind": outcome.kind,
                       "ref": outcome.ref})
    return outcome


def _json_list(rows: list[dict]) -> str:
    return json.dumps(rows, ensure_ascii=False, default=str)


def _append_pms_note(settings, reservation_ref: str, text: str, item: Item) -> None:
    """Best-effort PMS note. A read-only or unreachable PMS must never break
    a send. ``item`` is passed through to the write guard so an
    already-approved item can pass even in shadow mode (see
    ``core.review.APPROVED_STATES``)."""
    try:
        pms = get_pms(settings)
        pms.add_note(reservation_ref, text, item=item)
    except (AdapterError, WriteBlocked, NotImplementedError):
        pass


def _notify_staff(settings, summary: str, item: Item | None = None) -> dict:
    """Alert staff for an urgent guest request through the messaging
    adapter's ``notify_staff`` - a real write, guarded like any other send.
    ``item`` is passed through to the write guard - see ``_append_pms_note``."""
    try:
        messaging = get_messaging(settings)
        return {"ok": True, **messaging.notify_staff(summary, item=item)}
    except (AdapterError, WriteBlocked, NotImplementedError) as exc:
        return {"ok": False, "error": str(exc)}
