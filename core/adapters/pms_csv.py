"""Universal PMS adapter: CSV exports in, a "to apply by hand" log out.

Every PMS on earth can export a CSV. Drop the exports in ``data/imports/`` and
the agent reads them like any other PMS. This is the integration that always
works, and the one to start with on day one.

Expected files and headers (extra columns are kept, missing ones default):

``data/imports/reservations.csv``
    ``id, external_ref, status, check_in, check_out, room_type_id,
    room_type_name, room_id, adults, children, source, total, balance,
    currency, guest_email, guest_first_name, guest_last_name, guest_phone,
    guest_country, notes``
``data/imports/guests.csv``
    ``id, first_name, last_name, email, phone, country, language, vip, notes``
``data/imports/rooms.csv``
    ``id, name, max_occupancy, count, rank``
``data/imports/rates.csv``
    ``date, room_type_id, price, currency, min_los, available, closed``

Dates are ISO ``YYYY-MM-DD``. Header names are matched case-insensitively and
``camelCase``/``snake_case``/spaces are all accepted, so a raw PMS export
usually works unedited.

**Writes never touch the PMS.** ``set_rate``, ``add_note`` and friends append a
row to ``data/exports/pms_writes.csv`` with everything a person needs to apply
the change by hand. That is the honest behaviour for a system we cannot call.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.adapters.base import (Guest, HealthCheck, PMS, RateRow, Reservation, RoomType,
                                guarded_write)
from core.config import sub_data_dir

WRITE_LOG_HEADER = ["logged_at", "action", "target", "field", "value", "note"]


def _key(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _row_get(row: dict, *names: str, default: Any = "") -> Any:
    """Header lookup that ignores case, underscores and spaces."""
    normalised = {_key(k): v for k, v in row.items() if k}
    for name in names:
        value = normalised.get(_key(name))
        if value not in (None, ""):
            return value
    return default


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", ".").replace(" ", "") or default)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value) or default))
    except (TypeError, ValueError):
        return default


def _bool(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "y", "closed")


class CsvPMS(PMS):
    """Reads PMS exports from ``data/imports``. Works with any PMS."""

    status, name = "universal", "pms_csv"

    def __init__(self, settings: Any, config: Any = None) -> None:
        super().__init__(settings, config)
        configured = self.opt("imports_dir")
        self.dir = Path(configured) if configured else sub_data_dir("imports")
        self.write_log = sub_data_dir("exports") / "pms_writes.csv"

    # -- plumbing ---------------------------------------------------------
    def _read(self, filename: str) -> list[dict]:
        path = self.dir / filename
        if not path.exists():
            return []
        with path.open(newline="", encoding="utf-8-sig") as fh:
            return [dict(row) for row in csv.DictReader(fh)]

    def ping(self) -> HealthCheck:
        found = [f for f in ("reservations.csv", "guests.csv", "rooms.csv", "rates.csv")
                 if (self.dir / f).exists()]
        if not found:
            return HealthCheck(
                False, self.name, f"no CSV exports in {self.dir}",
                "Export reservations from your PMS and save it as "
                f"{self.dir}/reservations.csv (see docs/integrations.md).")
        n = len(self._read("reservations.csv"))
        return HealthCheck(True, self.name, f"{', '.join(found)} ({n} reservations)")

    def capabilities(self) -> set[str]:
        caps = {"list_arrivals", "list_departures", "list_in_house"}
        if (self.dir / "reservations.csv").exists():
            caps |= {"list_reservations", "get_reservation"}
        if (self.dir / "guests.csv").exists():
            caps |= {"find_guest", "get_guest"}
        if (self.dir / "rooms.csv").exists():
            caps.add("list_room_types")
        if (self.dir / "rates.csv").exists():
            caps |= {"get_rates", "get_availability"}
        return caps

    # -- reads ------------------------------------------------------------
    def _to_reservation(self, row: dict) -> Reservation:
        guest = Guest(
            id=str(_row_get(row, "guest_id", "guestID")),
            first_name=str(_row_get(row, "guest_first_name", "first_name", "firstName")),
            last_name=str(_row_get(row, "guest_last_name", "last_name", "lastName")),
            email=str(_row_get(row, "guest_email", "email")),
            phone=str(_row_get(row, "guest_phone", "phone")),
            country=str(_row_get(row, "guest_country", "country")),
            language=str(_row_get(row, "guest_language", "language")).lower(),
            vip=_bool(_row_get(row, "guest_vip", "vip")),
            # Guest-level facts a reservation export often carries (loyalty tier,
            # stay count, privacy wishes) must survive into the Guest record:
            # `notes` for free text, `extra` for every raw column.
            notes=str(_row_get(row, "guest_notes", "guest_note", "profile_notes")),
            extra=dict(row),
        )
        return Reservation(
            id=str(_row_get(row, "id", "reservation_id", "reservationID")),
            external_ref=str(_row_get(row, "external_ref", "confirmation", "booking_ref")),
            status=str(_row_get(row, "status", default="confirmed")),
            check_in=str(_row_get(row, "check_in", "checkin", "arrival", "startDate"))[:10],
            check_out=str(_row_get(row, "check_out", "checkout", "departure", "endDate"))[:10],
            room_type_id=str(_row_get(row, "room_type_id", "roomTypeID")),
            room_type_name=str(_row_get(row, "room_type_name", "room_type", "roomTypeName")),
            room_id=str(_row_get(row, "room_id", "room", "roomName")),
            adults=_int(_row_get(row, "adults", default=2), 2),
            children=_int(_row_get(row, "children")),
            source=str(_row_get(row, "source", "channel")),
            total=_num(_row_get(row, "total", "total_amount", "grandTotal")),
            balance=_num(_row_get(row, "balance", "balance_due")),
            currency=str(_row_get(row, "currency", default=self.settings.hotel.currency)),
            guest=guest, notes=str(_row_get(row, "notes")), extra=row)

    def list_reservations(self, date_from: str, date_to: str,
                          status: str | None = None) -> list[Reservation]:
        out = []
        for row in self._read("reservations.csv"):
            res = self._to_reservation(row)
            if not res.check_in:
                continue
            if res.check_out and res.check_out < date_from:
                continue
            if res.check_in > date_to:
                continue
            if status and res.status.lower() != status.lower():
                continue
            out.append(res)
        return out

    def get_reservation(self, reservation_id: str) -> Reservation | None:
        for row in self._read("reservations.csv"):
            res = self._to_reservation(row)
            if res.id == str(reservation_id) or res.external_ref == str(reservation_id):
                return res
        return None

    def _to_guest(self, row: dict) -> Guest:
        return Guest(
            id=str(_row_get(row, "id", "guest_id")),
            first_name=str(_row_get(row, "first_name", "firstName", "given_name")),
            last_name=str(_row_get(row, "last_name", "lastName", "surname")),
            email=str(_row_get(row, "email")), phone=str(_row_get(row, "phone", "mobile")),
            country=str(_row_get(row, "country")), language=str(_row_get(row, "language")),
            vip=_bool(_row_get(row, "vip")), notes=str(_row_get(row, "notes")), extra=row)

    def find_guest(self, email: str = "", phone: str = "", name: str = "") -> list[Guest]:
        needle = (name or "").strip().lower()
        out = []
        for row in self._read("guests.csv"):
            guest = self._to_guest(row)
            if email and guest.email.lower() == email.lower():
                out.append(guest)
            elif phone and guest.phone.replace(" ", "") == phone.replace(" ", ""):
                out.append(guest)
            elif needle and needle in guest.full_name.lower():
                out.append(guest)
        return out

    def get_guest(self, guest_id: str) -> Guest | None:
        for row in self._read("guests.csv"):
            guest = self._to_guest(row)
            if guest.id == str(guest_id):
                return guest
        return None

    def list_room_types(self) -> list[RoomType]:
        return [RoomType(id=str(_row_get(r, "id", "room_type_id")),
                         name=str(_row_get(r, "name", "room_type_name")),
                         max_occupancy=_int(_row_get(r, "max_occupancy", default=2), 2),
                         count=_int(_row_get(r, "count", "rooms")),
                         rank=_int(_row_get(r, "rank")), extra=r)
                for r in self._read("rooms.csv")]

    def get_rates(self, date_from: str, date_to: str,
                  room_type: str | None = None) -> list[RateRow]:
        out = []
        for row in self._read("rates.csv"):
            date = str(_row_get(row, "date", "day"))[:10]
            if not (date_from <= date <= date_to):
                continue
            rtid = str(_row_get(row, "room_type_id", "roomTypeID", "room_type"))
            if room_type is not None and rtid != room_type:
                continue
            out.append(RateRow(
                date=date, room_type_id=rtid, price=_num(_row_get(row, "price", "rate")),
                currency=str(_row_get(row, "currency", default=self.settings.hotel.currency)),
                min_los=_int(_row_get(row, "min_los", "minLos", default=1), 1),
                available=_int(_row_get(row, "available", "rooms_available")),
                closed=_bool(_row_get(row, "closed")), extra=row))
        return out

    def get_availability(self, date_from: str, date_to: str) -> list[RateRow]:
        return [r for r in self.get_rates(date_from, date_to) if r.available > 0 and not r.closed]

    # -- writes: a to-do list for a human --------------------------------
    def _log(self, action: str, target: str, field: str, value: Any, note: str = "") -> dict:
        new = not self.write_log.exists()
        self.write_log.parent.mkdir(parents=True, exist_ok=True)
        with self.write_log.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new:
                writer.writerow(WRITE_LOG_HEADER)
            writer.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             action, target, field, value, note])
        return {"ok": True, "applied": False, "logged_to": str(self.write_log),
                "note": "CSV mode cannot write to your PMS. Apply this row by hand."}

    @guarded_write("pms_write")
    def set_rate(self, date: str, room_type: str, price: float) -> dict:
        return self._log("set_rate", f"{date}/{room_type}", "price", price)

    @guarded_write("pms_write")
    def add_note(self, reservation_id: str, text: str) -> dict:
        return self._log("add_note", reservation_id, "note", text)

    @guarded_write("pms_write")
    def update_reservation(self, reservation_id: str, patch: dict) -> dict:
        for field, value in (patch or {}).items():
            self._log("update_reservation", reservation_id, field, value)
        return {"ok": True, "applied": False, "logged_to": str(self.write_log),
                "fields": list((patch or {}).keys())}

    @guarded_write("pms_write")
    def set_room_status(self, room: str, status: str) -> dict:
        return self._log("set_room_status", room, "status", status)
