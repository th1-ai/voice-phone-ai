"""Generic PMS bridge over a JSON-speaking command line tool — ADVANCED.

Some property systems already have a good CLI (or you can write a five-line
wrapper around one). This adapter shells out to it, asks for JSON, and maps the
result onto the standard :class:`~core.adapters.base.PMS` interface.

Use it when: your PMS has a CLI or an SDK you would rather not re-implement in
Python, or you already run a vendor tool on this machine.

Do not use it when: you can export CSV (use ``pms_csv``, it is simpler) or the
PMS is Cloudbeds (use ``pms_cloudbeds``).

Configure it in ``config/hotel.yaml``::

    systems:
      pms:
        adapter: cli
        command: mews-cli            # the executable, on PATH or an absolute path
        profile: mews                # which command-name map below to use
        timeout: 60
        env:                         # optional extra environment for the child
          MEWS_ENV: production

The bridge runs ``<command> <group> <cmd> --json [--key value ...]`` and expects
a JSON array or object on stdout. Non-zero exit or unparseable output raises,
with the tool's stderr included so you can see what it complained about.

If your CLI names things differently, add a profile to :data:`PROFILES` — that
is a dict of ``{operation: (group, command)}`` and nothing else. The field
mapping is deliberately forgiving: it accepts snake_case, camelCase and a few
common aliases, so most CLIs work without a mapping layer.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

from core.adapters.base import (AdapterNotConfigured, AdapterNotImplemented, Guest,
                                HealthCheck, PMS, RateRow, Reservation, RoomType,
                                guarded_write)
from core.adapters.pms_csv import _bool, _int, _num, _row_get

#: ``{profile: {operation: (group, command)}}``. Add yours here.
PROFILES: dict[str, dict[str, tuple[str, str]]] = {
    "generic": {
        "list_reservations": ("reservations", "list"),
        "get_reservation": ("reservations", "get"),
        "find_guest": ("guests", "search"),
        "get_guest": ("guests", "get"),
        "list_room_types": ("rooms", "types"),
        "get_rates": ("rates", "list"),
        "get_availability": ("availability", "list"),
        "set_rate": ("rates", "set"),
        "add_note": ("reservations", "add-note"),
        "update_reservation": ("reservations", "update"),
    },
    "cloudbeds": {
        "list_reservations": ("reservations", "list"),
        "get_reservation": ("reservations", "get"),
        "find_guest": ("guests", "list"),
        "get_guest": ("guests", "get"),
        "list_room_types": ("room-types", "list"),
        "get_rates": ("rate-plans", "list"),
        "get_availability": ("availability", "get"),
        "set_rate": ("rates", "put"),
        "add_note": ("reservations", "post-note"),
        "update_reservation": ("reservations", "put"),
    },
    "mews": {
        "list_reservations": ("reservations", "get-all"),
        "get_reservation": ("reservations", "get"),
        "find_guest": ("customers", "search"),
        "get_guest": ("customers", "get"),
        "list_room_types": ("resource-categories", "get-all"),
        "get_rates": ("rates", "get-pricing"),
        "get_availability": ("services", "get-availability"),
        "set_rate": ("rates", "update-price"),
        "add_note": ("reservations", "add-note"),
        "update_reservation": ("reservations", "update"),
    },
    "trackhs": {
        "list_reservations": ("reservations", "search"),
        "get_reservation": ("reservations", "get"),
        "find_guest": ("contacts", "search"),
        "get_guest": ("contacts", "get"),
        "list_room_types": ("unit-types", "list"),
        "get_rates": ("rates", "list"),
        "get_availability": ("availability", "list"),
        "set_rate": ("rates", "update"),
        "add_note": ("reservations", "note"),
        "update_reservation": ("reservations", "update"),
    },
    "optima-pms": {
        "list_reservations": ("reservation", "list"),
        "get_reservation": ("reservation", "get"),
        "find_guest": ("profile", "search"),
        "get_guest": ("profile", "get"),
        "list_room_types": ("roomtype", "list"),
        "get_rates": ("rate", "list"),
        "get_availability": ("availability", "get"),
        "set_rate": ("rate", "update"),
        "add_note": ("reservation", "comment"),
        "update_reservation": ("reservation", "update"),
    },
    "stayntouch": {
        "list_reservations": ("reservations", "list"),
        "get_reservation": ("reservations", "show"),
        "find_guest": ("guests", "search"),
        "get_guest": ("guests", "show"),
        "list_room_types": ("room-types", "list"),
        "get_rates": ("rates", "list"),
        "get_availability": ("inventory", "list"),
        "set_rate": ("rates", "update"),
        "add_note": ("reservations", "add-note"),
        "update_reservation": ("reservations", "update"),
    },
    "opera-cloud": {
        "list_reservations": ("reservations", "search"),
        "get_reservation": ("reservations", "get"),
        "find_guest": ("profiles", "search"),
        "get_guest": ("profiles", "get"),
        "list_room_types": ("room-types", "list"),
        "get_rates": ("rate-plans", "list"),
        "get_availability": ("availability", "get"),
        "set_rate": ("rate-plans", "update"),
        "add_note": ("reservations", "add-note"),
        "update_reservation": ("reservations", "modify"),
    },
}


class CliPMS(PMS):
    """Bridges to any CLI that can print JSON. Advanced; prefer csv or a built adapter."""

    status, name = "universal", "pms_cli"

    def __init__(self, settings: Any, config: Any = None) -> None:
        super().__init__(settings, config)
        self.command = str(self.opt("command", "", env="PMS_CLI_COMMAND"))
        self.profile = str(self.opt("profile", "generic", env="PMS_CLI_PROFILE"))
        self.timeout = int(self.opt("timeout", 60) or 60)
        self.extra_env = dict(self.opt("env", {}) or {})
        self.map = PROFILES.get(self.profile, PROFILES["generic"])

    # -- plumbing ---------------------------------------------------------
    def _run(self, operation: str, **flags: Any) -> Any:
        if not self.command:
            raise AdapterNotConfigured(
                "pms_cli: systems.pms.command is not set in config/hotel.yaml. "
                "Point it at your PMS command line tool.")
        if not shutil.which(self.command) and not os.path.exists(self.command):
            raise AdapterNotConfigured(
                f"pms_cli: '{self.command}' is not on PATH. Install it, or use "
                "systems.pms.adapter: csv instead.")
        pair = self.map.get(operation)
        if pair is None:
            raise AdapterNotImplemented(
                f"pms_cli[{self.profile}]", method=operation)
        argv = [self.command, pair[0], pair[1], "--json"]
        for key, value in flags.items():
            if value in (None, ""):
                continue
            argv += [f"--{key.replace('_', '-')}", str(value)]
        env = {**os.environ, **{k: str(v) for k, v in self.extra_env.items()}}
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout,
                              env=env)
        if proc.returncode != 0:
            raise AdapterNotConfigured(
                f"pms_cli: `{' '.join(argv[:4])}` exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout).strip()[:300]}")
        try:
            return json.loads(proc.stdout or "null")
        except json.JSONDecodeError as exc:
            raise AdapterNotConfigured(
                f"pms_cli: `{' '.join(argv[:4])}` did not print JSON: "
                f"{proc.stdout[:200]}") from exc

    @staticmethod
    def _rows(payload: Any) -> list[dict]:
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if isinstance(payload, dict):
            for key in ("data", "items", "results", "reservations", "rows"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [r for r in value if isinstance(r, dict)]
            return [payload]
        return []

    # -- introspection ----------------------------------------------------
    def ping(self) -> HealthCheck:
        if not self.command:
            return HealthCheck(False, self.name, "systems.pms.command not set",
                               "Set it in config/hotel.yaml, or use adapter: csv.")
        if not shutil.which(self.command) and not os.path.exists(self.command):
            return HealthCheck(False, self.name, f"'{self.command}' not found on PATH",
                               "Install the CLI or correct systems.pms.command.")
        try:
            rows = self._rows(self._run("list_room_types"))
        except Exception as exc:  # noqa: BLE001 - ping never raises
            return HealthCheck(False, self.name, str(exc)[:200],
                               "Run the command by hand to see what it needs.")
        return HealthCheck(True, self.name,
                           f"{self.command} [{self.profile}] responded, {len(rows)} room types")

    def capabilities(self) -> set[str]:
        return set(self.map) | {"list_arrivals", "list_departures", "list_in_house"}

    # -- reads ------------------------------------------------------------
    def _to_reservation(self, row: dict) -> Reservation:
        return Reservation(
            id=str(_row_get(row, "id", "reservation_id", "reservationID")),
            external_ref=str(_row_get(row, "external_ref", "confirmation_number")),
            status=str(_row_get(row, "status", default="confirmed")),
            check_in=str(_row_get(row, "check_in", "arrival", "startDate"))[:10],
            check_out=str(_row_get(row, "check_out", "departure", "endDate"))[:10],
            room_type_id=str(_row_get(row, "room_type_id", "roomTypeID", "categoryId")),
            room_type_name=str(_row_get(row, "room_type_name", "roomTypeName")),
            room_id=str(_row_get(row, "room_id", "roomID", "resourceId")),
            adults=_int(_row_get(row, "adults", default=2), 2),
            children=_int(_row_get(row, "children")),
            source=str(_row_get(row, "source", "channel")),
            total=_num(_row_get(row, "total", "grandTotal")),
            balance=_num(_row_get(row, "balance", "balanceDue")),
            currency=str(_row_get(row, "currency", default=self.settings.hotel.currency)),
            guest=Guest(
                id=str(_row_get(row, "guest_id", "guestID", "customerId")),
                first_name=str(_row_get(row, "guest_first_name", "firstName")),
                last_name=str(_row_get(row, "guest_last_name", "lastName")),
                email=str(_row_get(row, "guest_email", "email")),
                phone=str(_row_get(row, "guest_phone", "phone")),
                country=str(_row_get(row, "guest_country", "country"))),
            notes=str(_row_get(row, "notes")), extra=row)

    def list_reservations(self, date_from: str, date_to: str,
                          status: str | None = None) -> list[Reservation]:
        rows = self._rows(self._run("list_reservations", start=date_from, end=date_to,
                                    status=status))
        return [self._to_reservation(r) for r in rows]

    def get_reservation(self, reservation_id: str) -> Reservation | None:
        rows = self._rows(self._run("get_reservation", id=reservation_id))
        return self._to_reservation(rows[0]) if rows else None

    def find_guest(self, email: str = "", phone: str = "", name: str = "") -> list[Guest]:
        rows = self._rows(self._run("find_guest", email=email, phone=phone, name=name))
        return [Guest(id=str(_row_get(r, "id", "guestID", "customerId")),
                      first_name=str(_row_get(r, "first_name", "firstName")),
                      last_name=str(_row_get(r, "last_name", "lastName")),
                      email=str(_row_get(r, "email")), phone=str(_row_get(r, "phone")),
                      country=str(_row_get(r, "country")), extra=r) for r in rows]

    def get_guest(self, guest_id: str) -> Guest | None:
        rows = self._rows(self._run("get_guest", id=guest_id))
        if not rows:
            return None
        r = rows[0]
        return Guest(id=str(_row_get(r, "id", "guestID")),
                     first_name=str(_row_get(r, "first_name", "firstName")),
                     last_name=str(_row_get(r, "last_name", "lastName")),
                     email=str(_row_get(r, "email")), phone=str(_row_get(r, "phone")),
                     country=str(_row_get(r, "country")), extra=r)

    def list_room_types(self) -> list[RoomType]:
        return [RoomType(id=str(_row_get(r, "id", "roomTypeID", "categoryId")),
                         name=str(_row_get(r, "name", "roomTypeName")),
                         max_occupancy=_int(_row_get(r, "max_occupancy", "maxGuests",
                                                     default=2), 2),
                         count=_int(_row_get(r, "count", "units")), extra=r)
                for r in self._rows(self._run("list_room_types"))]

    def get_rates(self, date_from: str, date_to: str,
                  room_type: str | None = None) -> list[RateRow]:
        rows = self._rows(self._run("get_rates", start=date_from, end=date_to,
                                    room_type=room_type))
        return [RateRow(date=str(_row_get(r, "date", "day"))[:10],
                        room_type_id=str(_row_get(r, "room_type_id", "roomTypeID")),
                        price=_num(_row_get(r, "price", "rate", "amount")),
                        currency=str(_row_get(r, "currency",
                                              default=self.settings.hotel.currency)),
                        min_los=_int(_row_get(r, "min_los", "minLos", default=1), 1),
                        available=_int(_row_get(r, "available", "roomsAvailable")),
                        closed=_bool(_row_get(r, "closed")), extra=r) for r in rows]

    def get_availability(self, date_from: str, date_to: str) -> list[RateRow]:
        rows = self._rows(self._run("get_availability", start=date_from, end=date_to))
        return [RateRow(date=str(_row_get(r, "date", "day"))[:10],
                        room_type_id=str(_row_get(r, "room_type_id", "roomTypeID")),
                        price=_num(_row_get(r, "price", "rate")),
                        available=_int(_row_get(r, "available", "roomsAvailable")),
                        extra=r) for r in rows]

    # -- writes (guarded) --------------------------------------------------
    @guarded_write("pms_write")
    def set_rate(self, date: str, room_type: str, price: float) -> dict:
        return {"result": self._run("set_rate", date=date, room_type=room_type, price=price)}

    @guarded_write("pms_write")
    def add_note(self, reservation_id: str, text: str) -> dict:
        return {"result": self._run("add_note", id=reservation_id, text=text)}

    @guarded_write("pms_write")
    def update_reservation(self, reservation_id: str, patch: dict) -> dict:
        return {"result": self._run("update_reservation", id=reservation_id,
                                    patch=json.dumps(patch or {}, ensure_ascii=False))}
