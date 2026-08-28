"""PMS adapter backed by ``fixtures/hotel/*.json`` — no credentials, no network.

This is what ``make demo`` and the tests run on. It reads four optional files:

``fixtures/hotel/reservations.json``  list of reservation objects
``fixtures/hotel/guests.json``        list of guest objects
``fixtures/hotel/room_types.json``    list of room type objects
``fixtures/hotel/rates.json``         list of ``{date, room_type_id, price, available}``

Field names match the dataclasses in :mod:`core.adapters.base`; anything extra is
kept in ``.extra``. Writes are guarded like any other adapter and are appended to
``data/exports/pms_writes.csv`` so you can see what the agent *would* have done.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from core.adapters.base import (Guest, HealthCheck, PMS, RateRow, Reservation, RoomType,
                                guarded_write)
from core.config import repo_root, sub_data_dir


def _guest(raw: dict) -> Guest:
    known = {"id", "first_name", "last_name", "email", "phone", "country", "language",
             "vip", "notes"}
    return Guest(**{k: v for k, v in raw.items() if k in known},
                 extra={k: v for k, v in raw.items() if k not in known})


def _reservation(raw: dict) -> Reservation:
    known = {"id", "external_ref", "status", "check_in", "check_out", "room_type_id",
             "room_type_name", "room_id", "adults", "children", "source", "total",
             "balance", "currency", "notes"}
    guest = raw.get("guest") or {}
    return Reservation(
        **{k: v for k, v in raw.items() if k in known},
        guest=_guest(guest if isinstance(guest, dict) else {}),
        extra={k: v for k, v in raw.items() if k not in known and k != "guest"})


class MockPMS(PMS):
    """Fixture-backed PMS. Always available, always the same answers."""

    status, name = "universal", "pms_mock"

    def __init__(self, settings: Any, config: Any = None) -> None:
        super().__init__(settings, config)
        self.dir = Path(self.opt("fixtures_dir") or (repo_root() / "fixtures" / "hotel"))
        self.write_log = sub_data_dir("exports") / "pms_writes.csv"

    # -- plumbing ---------------------------------------------------------
    def _load(self, name: str) -> list[dict]:
        path = self.dir / f"{name}.json"
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get(name) or data.get("data") or []
        return [d for d in data if isinstance(d, dict)]

    def ping(self) -> HealthCheck:
        if not self.dir.exists():
            return HealthCheck(False, self.name, f"no fixtures at {self.dir}",
                               "Create fixtures/hotel/reservations.json, or switch "
                               "systems.pms.adapter to csv.")
        n = len(self._load("reservations"))
        return HealthCheck(True, self.name, f"{n} fixture reservations in {self.dir}")

    def capabilities(self) -> set[str]:
        return {"list_reservations", "get_reservation", "find_guest", "get_guest",
                "list_room_types", "get_availability", "get_rates", "list_arrivals",
                "list_departures", "list_in_house", "add_note", "set_rate",
                "update_reservation"}

    # -- reads ------------------------------------------------------------
    def list_reservations(self, date_from: str, date_to: str,
                          status: str | None = None) -> list[Reservation]:
        out = []
        for raw in self._load("reservations"):
            res = _reservation(raw)
            if res.check_out < date_from or res.check_in > date_to:
                continue
            if status and res.status != status:
                continue
            out.append(res)
        return out

    def get_reservation(self, reservation_id: str) -> Reservation | None:
        for raw in self._load("reservations"):
            if str(raw.get("id")) == str(reservation_id):
                return _reservation(raw)
        return None

    def find_guest(self, email: str = "", phone: str = "", name: str = "") -> list[Guest]:
        needle_name = (name or "").strip().lower()
        out = []
        for raw in self._load("guests"):
            guest = _guest(raw)
            if email and guest.email.lower() == email.lower():
                out.append(guest)
            elif phone and guest.phone.replace(" ", "") == phone.replace(" ", ""):
                out.append(guest)
            elif needle_name and needle_name in guest.full_name.lower():
                out.append(guest)
        return out

    def get_guest(self, guest_id: str) -> Guest | None:
        for raw in self._load("guests"):
            if str(raw.get("id")) == str(guest_id):
                return _guest(raw)
        return None

    def list_room_types(self) -> list[RoomType]:
        return [RoomType(id=str(r.get("id", "")), name=str(r.get("name", "")),
                         max_occupancy=int(r.get("max_occupancy", 2)),
                         count=int(r.get("count", 0)), rank=int(r.get("rank", 0)),
                         extra=r) for r in self._load("room_types")]

    def _rates(self) -> list[RateRow]:
        return [RateRow(date=str(r.get("date", "")), room_type_id=str(r.get("room_type_id", "")),
                        price=float(r.get("price", 0) or 0),
                        currency=str(r.get("currency", self.settings.hotel.currency)),
                        min_los=int(r.get("min_los", 1)),
                        available=int(r.get("available", 0) or 0),
                        closed=bool(r.get("closed", False)))
                for r in self._load("rates")]

    def get_rates(self, date_from: str, date_to: str,
                  room_type: str | None = None) -> list[RateRow]:
        return [r for r in self._rates()
                if date_from <= r.date <= date_to
                and (room_type is None or r.room_type_id == room_type)]

    def get_availability(self, date_from: str, date_to: str) -> list[RateRow]:
        return [r for r in self.get_rates(date_from, date_to) if r.available > 0]

    def get_folio(self, reservation_id: str) -> dict:
        res = self.get_reservation(reservation_id)
        if res is None:
            return {}
        return {"reservation_id": res.id, "total": res.total, "balance": res.balance,
                "currency": res.currency, "lines": res.extra.get("folio", [])}

    # -- writes -----------------------------------------------------------
    def _log_write(self, action: str, target: str, detail: Any) -> dict:
        new = not self.write_log.exists()
        with self.write_log.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new:
                writer.writerow(["action", "target", "detail"])
            writer.writerow([action, target, json.dumps(detail, ensure_ascii=False,
                                                        default=str)])
        return {"ok": True, "logged_to": str(self.write_log), "action": action}

    @guarded_write("pms_write")
    def add_note(self, reservation_id: str, text: str) -> dict:
        return self._log_write("add_note", reservation_id, text)

    @guarded_write("pms_write")
    def set_rate(self, date: str, room_type: str, price: float) -> dict:
        return self._log_write("set_rate", f"{date}/{room_type}", price)

    @guarded_write("pms_write")
    def update_reservation(self, reservation_id: str, patch: dict) -> dict:
        return self._log_write("update_reservation", reservation_id, patch)

    @guarded_write("pms_write")
    def set_room_status(self, room: str, status: str) -> dict:
        return self._log_write("set_room_status", room, status)
