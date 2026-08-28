"""tools/pricing.py - pure functions: room rates, menu matching, table rules.

No I/O, no store, no settings object mutated. Every function here takes plain
values (or the small config dicts loaded from `config/agent.yaml`) and
returns a plain value, so `tests/test_voicephone_pricing.py` can check the
maths without a database or a fixture file. `tools/booking.py` is the only
caller - it does the I/O, this does the arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

DEFAULT_SEASON = [0.85, 0.85, 0.90, 1.00, 1.10, 1.25, 1.35, 1.35, 1.15, 1.00, 0.90, 0.95]


class UnknownRoomType(ValueError):
    """Raised with the valid list, so the caller can offer it instead of guessing."""


def parse_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def parse_time(value: str) -> tuple[int, int]:
    """``"HH:MM"`` -> ``(hour, minute)``. Raises ``ValueError`` on anything else."""
    text = str(value).strip()
    hour, _, minute = text.partition(":")
    h, m = int(hour), int(minute or 0)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"'{value}' is not a valid HH:MM time")
    return h, m


def minutes_since_midnight(value: str) -> int:
    h, m = parse_time(value)
    return h * 60 + m


# --------------------------------------------------------------------------
# rooms
# --------------------------------------------------------------------------
def room_type_list(room_types: dict) -> str:
    """``slug (Display Name)`` for every configured room type - what the
    classify prompt shows the model, and what an error offers a caller."""
    return ", ".join(f"{slug} ({cfg.get('name', slug)})" for slug, cfg in room_types.items())


def match_room_type(query: str, room_types: dict) -> str:
    """Fuzzy-match a caller's (or the model's) words to a room-type slug.

    Order: exact slug, then exact display name, then a substring match
    either way against the display name, then a substring/prefix match
    against the slug with hyphens treated as spaces (so "Sea View" reaches
    ``sea-view``). Raises :class:`UnknownRoomType` naming every slug
    alongside its display name when nothing matches.
    """
    needle = (query or "").strip().lower()
    if not needle:
        raise UnknownRoomType("no room type given. Known: " + room_type_list(room_types))
    for slug in room_types:
        if slug.lower() == needle:
            return slug
    for slug, cfg in room_types.items():
        if str(cfg.get("name", "")).strip().lower() == needle:
            return slug
    for slug, cfg in room_types.items():
        name = str(cfg.get("name", "")).strip().lower()
        if name and (needle in name or name in needle):
            return slug
    for slug in room_types:
        norm = slug.lower().replace("-", " ")
        if norm and (norm in needle or needle in norm
                    or norm.startswith(needle) or needle.startswith(norm)):
            return slug
    raise UnknownRoomType(
        f"unknown room type '{query}'. Known: " + room_type_list(room_types))


def nightly_rate(room_type: str, day: date, room_types: dict, *,
                 season: list[float] | None = None, weekend_multiplier: float = 1.08,
                 weekend_days: tuple[int, ...] = (4, 5)) -> float:
    """One night's rate: base x season[month] x weekend, rounded to the nearest 5."""
    cfg = room_types.get(room_type)
    if cfg is None:
        raise UnknownRoomType(
            f"unknown room type '{room_type}'. Known: {', '.join(sorted(room_types))}")
    season = season or DEFAULT_SEASON
    base = float(cfg["base_rate"])
    factor = season[day.month - 1] * (weekend_multiplier if day.weekday() in weekend_days else 1.0)
    return round(base * factor / 5) * 5


def stay_total(room_type: str, checkin: str, checkout: str, room_types: dict, **kwargs) -> float:
    """Sum of :func:`nightly_rate` over every night of the stay."""
    start, end = parse_date(checkin), parse_date(checkout)
    if end <= start:
        raise ValueError("checkout must be after checkin")
    total, day = 0.0, start
    while day < end:
        total += nightly_rate(room_type, day, room_types, **kwargs)
        day = date.fromordinal(day.toordinal() + 1)
    return total


def nights_between(checkin: str, checkout: str) -> int:
    return (parse_date(checkout) - parse_date(checkin)).days


def is_large_group(guests: int, large_group_pax: int = 6) -> bool:
    return guests >= large_group_pax


# --------------------------------------------------------------------------
# restaurant
# --------------------------------------------------------------------------
def is_service_closed(day: date, closed_weekdays: list[int]) -> bool:
    return day.weekday() in (closed_weekdays or [])


def is_within_service_hours(time_str: str, service_start: str, service_end: str) -> bool:
    return minutes_since_midnight(service_start) <= minutes_since_midnight(time_str) \
        <= minutes_since_midnight(service_end)


def is_large_party(party_size: int, large_party_size: int = 7) -> bool:
    return party_size >= large_party_size


def seating_window(time_str: str, window_minutes: int = 90) -> tuple[str, str]:
    """The ``(start, end)`` ``HH:MM`` bounds of the seating window around one
    booking time, clamped to the same day, for a store-side overlap query -
    see ``tools/store_ext.py:count_table_covers_in_window``. A restaurant's
    service hours never run past midnight in this template, so clamping
    (rather than wrapping) the window at the day's edges is the right
    behaviour here."""
    anchor = datetime(2000, 1, 1, *parse_time(time_str))
    half = timedelta(minutes=window_minutes)
    start = max(anchor - half, datetime(2000, 1, 1, 0, 0))
    end = min(anchor + half, datetime(2000, 1, 1, 23, 59))
    return start.strftime("%H:%M"), end.strftime("%H:%M")


# --------------------------------------------------------------------------
# room service
# --------------------------------------------------------------------------
@dataclass
class MenuMatch:
    item_id: str
    title: str
    price: float
    qty: int
    line_total: float


@dataclass
class OrderTotal:
    matched: list[MenuMatch] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    items_total: float = 0.0
    tray_charge: float = 0.0

    @property
    def all_matched(self) -> bool:
        return not self.unmatched and bool(self.matched)

    @property
    def grand_total(self) -> float:
        return self.items_total + self.tray_charge


def match_menu_item(query: str, menu: list[dict]) -> dict | None:
    """Fuzzy-match one caller phrase against the room-service menu.

    Order: exact title (case-insensitive), then a substring match either
    way, then the per-item ``hints`` keywords. Returns ``None`` (not an
    exception) when nothing matches, so a caller's multi-item order can
    report the specific items that need a human rather than aborting the
    whole thing - see ``tools/booking.py:preview_room_service``.
    """
    needle = (query or "").strip().lower()
    if not needle:
        return None
    for item in menu:
        if item["title"].strip().lower() == needle:
            return item
    for item in menu:
        title = item["title"].strip().lower()
        if needle in title or title in needle:
            return item
    for item in menu:
        for hint in item.get("hints", []):
            if hint.lower() in needle:
                return item
    return None


def price_order(items: list[dict], menu: list[dict], tray_charge: float) -> OrderTotal:
    """Price a list of ``{"name": ..., "qty": ...}`` against the menu.

    Every matched line is ``price x qty``; an item nothing on the menu
    matches is reported by name in ``unmatched`` rather than silently
    dropped or priced at zero. The tray charge is added once, regardless of
    how many items are ordered, matching the source spec.
    """
    out = OrderTotal(tray_charge=float(tray_charge))
    for raw in items:
        name = str(raw.get("name", "")).strip()
        qty = max(1, int(raw.get("qty") or 1))
        hit = match_menu_item(name, menu)
        if hit is None:
            out.unmatched.append(name)
            continue
        price = float(hit["price"])
        match = MenuMatch(item_id=hit["id"], title=hit["title"], price=price, qty=qty,
                          line_total=price * qty)
        out.matched.append(match)
        out.items_total += match.line_total
    return out


# --------------------------------------------------------------------------
# references
# --------------------------------------------------------------------------
def format_ref(prefix: str, seed: int) -> str:
    """A short, human-readable booking reference. ``seed`` should vary per
    call - callers own how they generate it (see ``tools/booking.py``)."""
    return f"{prefix}-{seed:04d}"
