"""Cloudbeds PMS adapter — status: built.

Written against the Cloudbeds v1.2 API (with v1.3 for room assignment). It is a
real integration: the reads are live and the writes really change the PMS, which
is exactly why every write goes through the shadow-mode guard first.

**Credentials.** Cloudbeds uses OAuth. You need an app in the Cloudbeds developer
portal with a redirect URI you control, then a one-time authorisation to get a
refresh token. Put these in ``.env``::

    CLOUDBEDS_CLIENT_ID=...
    CLOUDBEDS_CLIENT_SECRET=...
    CLOUDBEDS_REFRESH_TOKEN=...
    CLOUDBEDS_PROPERTY_ID=...          # numeric, from getHotels
    CLOUDBEDS_ACCESS_TOKEN=            # optional, refreshed automatically
    CLOUDBEDS_HOST=hotels.cloudbeds.com

The access token is short lived. This adapter refreshes it on a 401 and caches
the new one in ``data/.cloudbeds_token.json`` (gitignored), so a cron run does
not re-authorise every time.

**Rate limit.** Cloudbeds allows about 4 calls a second. A sliding-window limiter
enforces that locally, and 429/5xx responses are retried with backoff.

**Scopes** your app needs: ``read:reservation``, ``write:reservation``,
``read:guest``, ``read:room``, ``read:rate``, ``write:rate``, ``read:hotel``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from core.adapters._http import HttpError, RateLimiter, request_json
from core.adapters.base import (AdapterNotConfigured, Guest, HealthCheck, PMS, RateRow,
                                Reservation, RoomType, guarded_write)
from core.config import data_dir

_LIMITER = RateLimiter(max_calls=4, period=1.0)


class CloudbedsPMS(PMS):
    """Live Cloudbeds integration. Reads are safe; writes are guarded."""

    status, name = "built", "pms_cloudbeds"

    def __init__(self, settings: Any, config: Any = None) -> None:
        super().__init__(settings, config)
        self.host = str(self.opt("host", "hotels.cloudbeds.com", env="CLOUDBEDS_HOST"))
        self.base = f"https://{self.host}/api/v1.2"
        self.base_v13 = f"https://{self.host}/api/v1.3"
        self.property_id = str(self.opt("property_id", "", env="CLOUDBEDS_PROPERTY_ID"))
        self._token_cache = data_dir() / ".cloudbeds_token.json"
        self._token: str | None = None

    # -- auth -------------------------------------------------------------
    def _cached_token(self) -> str | None:
        if self._token:
            return self._token
        if self._token_cache.exists():
            try:
                blob = json.loads(self._token_cache.read_text(encoding="utf-8"))
                if float(blob.get("expires_at", 0)) > time.time() + 60:
                    self._token = str(blob.get("access_token") or "") or None
                    return self._token
            except (ValueError, OSError):
                pass
        env_token = os.environ.get("CLOUDBEDS_ACCESS_TOKEN")
        if env_token:
            self._token = env_token
            return self._token
        return None

    def _refresh(self) -> str:
        """Exchange the refresh token for a new access token and cache it."""
        client_id = os.environ.get("CLOUDBEDS_CLIENT_ID")
        client_secret = os.environ.get("CLOUDBEDS_CLIENT_SECRET")
        refresh_token = os.environ.get("CLOUDBEDS_REFRESH_TOKEN")
        if not (client_id and client_secret and refresh_token):
            raise AdapterNotConfigured(
                "pms_cloudbeds: need CLOUDBEDS_CLIENT_ID, CLOUDBEDS_CLIENT_SECRET and "
                "CLOUDBEDS_REFRESH_TOKEN in .env. See docs/integrations.md for how to "
                "get them from the Cloudbeds developer portal.")
        _LIMITER.wait()
        data = request_json("POST", f"{self.base}/access_token", form={
            "grant_type": "refresh_token", "client_id": client_id,
            "client_secret": client_secret, "refresh_token": refresh_token})
        token = str(data.get("access_token") or "")
        if not token:
            raise AdapterNotConfigured(f"pms_cloudbeds: token refresh returned no token: {data}")
        self._token = token
        try:
            self._token_cache.write_text(json.dumps({
                "access_token": token,
                "expires_at": time.time() + float(data.get("expires_in") or 3500),
            }), encoding="utf-8")
            self._token_cache.chmod(0o600)
        except OSError:
            pass  # cache is an optimisation, not a requirement
        return token

    def _headers(self) -> dict:
        token = self._cached_token() or self._refresh()
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _call(self, method: str, endpoint: str, *, base: str | None = None,
              params: dict | None = None, form: dict | None = None,
              json_body: Any = None) -> dict:
        """One API call, with a refresh-and-retry on 401 and the rate limiter."""
        url = f"{base or self.base}/{endpoint}"
        payload = dict(params or {})
        if self.property_id and method.upper() == "GET":
            payload.setdefault("propertyID", self.property_id)
        body = dict(form) if form is not None else None
        if body is not None and self.property_id:
            body.setdefault("propertyID", self.property_id)
        for attempt in (0, 1):
            _LIMITER.wait()
            try:
                return request_json(method, url, headers=self._headers(),
                                    params=payload or None, form=body, json_body=json_body)
            except HttpError as exc:
                if exc.status == 401 and attempt == 0:
                    self._token = None
                    self._refresh()
                    continue
                raise
        raise HttpError(0, url, "unreachable")  # pragma: no cover

    # -- introspection ----------------------------------------------------
    def ping(self) -> HealthCheck:
        missing = [v for v in ("CLOUDBEDS_CLIENT_ID", "CLOUDBEDS_CLIENT_SECRET",
                               "CLOUDBEDS_REFRESH_TOKEN") if not os.environ.get(v)]
        if missing:
            return HealthCheck(False, self.name, f"missing {', '.join(missing)}",
                               "Add them to .env — see docs/integrations.md#pms.")
        try:
            data = self._call("GET", "getHotels")
        except (HttpError, AdapterNotConfigured) as exc:
            return HealthCheck(False, self.name, str(exc)[:200],
                               "Check the client id/secret and that the refresh token "
                               "has not been revoked.")
        hotels = data.get("data") or []
        if not self.property_id and len(hotels) == 1:
            self.property_id = str(hotels[0].get("propertyID", ""))
        return HealthCheck(True, self.name,
                           f"connected, {len(hotels)} property/properties, "
                           f"propertyID={self.property_id or 'unset'}",
                           "" if self.property_id else
                           "Set CLOUDBEDS_PROPERTY_ID — you have more than one property.")

    def capabilities(self) -> set[str]:
        return {"list_reservations", "get_reservation", "find_guest", "get_guest",
                "list_room_types", "get_availability", "get_rates", "set_rate",
                "add_note", "update_reservation", "list_arrivals", "list_departures",
                "list_in_house", "get_folio", "list_housekeeping"}

    # -- mapping ----------------------------------------------------------
    def _to_guest(self, raw: dict) -> Guest:
        return Guest(
            id=str(raw.get("guestID") or raw.get("guestId") or ""),
            first_name=str(raw.get("guestFirstName") or raw.get("firstName") or ""),
            last_name=str(raw.get("guestLastName") or raw.get("lastName") or ""),
            email=str(raw.get("guestEmail") or raw.get("email") or ""),
            phone=str(raw.get("guestPhone") or raw.get("phone") or
                      raw.get("guestCellPhone") or ""),
            country=str(raw.get("guestCountry") or raw.get("country") or ""),
            extra=raw)

    def _to_reservation(self, raw: dict) -> Reservation:
        rooms = raw.get("rooms") or raw.get("assigned") or []
        first = rooms[0] if rooms and isinstance(rooms[0], dict) else {}
        return Reservation(
            id=str(raw.get("reservationID") or ""),
            external_ref=str(raw.get("thirdPartyIdentifier") or ""),
            status=str(raw.get("status") or "confirmed"),
            check_in=str(raw.get("startDate") or "")[:10],
            check_out=str(raw.get("endDate") or "")[:10],
            room_type_id=str(first.get("roomTypeID") or raw.get("roomTypeID") or ""),
            room_type_name=str(first.get("roomTypeName") or raw.get("roomTypeName") or ""),
            room_id=str(first.get("roomID") or raw.get("roomID") or ""),
            adults=int(raw.get("adults") or first.get("adults") or 2),
            children=int(raw.get("children") or first.get("children") or 0),
            source=str(raw.get("sourceName") or raw.get("source") or ""),
            total=float(raw.get("total") or raw.get("grandTotal") or 0) or 0.0,
            balance=float(raw.get("balance") or raw.get("balanceDue") or 0) or 0.0,
            currency=str(raw.get("currency") or self.settings.hotel.currency),
            guest=self._to_guest(raw), extra=raw)

    # -- reads ------------------------------------------------------------
    def list_reservations(self, date_from: str, date_to: str,
                          status: str | None = None) -> list[Reservation]:
        """Reservations overlapping the window (Cloudbeds ``getReservations``)."""
        params: dict[str, Any] = {"checkInFrom": date_from, "checkOutTo": date_to,
                                  "pageNumber": 1, "pageSize": 100}
        if status:
            params["status"] = status
        out: list[Reservation] = []
        while True:
            data = self._call("GET", "getReservations", params=params)
            rows = data.get("data") or []
            out += [self._to_reservation(r) for r in rows if isinstance(r, dict)]
            if len(rows) < params["pageSize"] or params["pageNumber"] > 20:
                break
            params["pageNumber"] += 1
        return out

    def get_reservation(self, reservation_id: str) -> Reservation | None:
        data = self._call("GET", "getReservation", params={"reservationID": reservation_id})
        raw = data.get("data")
        return self._to_reservation(raw) if isinstance(raw, dict) else None

    def find_guest(self, email: str = "", phone: str = "", name: str = "") -> list[Guest]:
        """Guest lookup chain: email, then phone, then name. First hit wins."""
        for key, value in (("email", email), ("phone", phone), ("guestName", name)):
            if not value:
                continue
            data = self._call("GET", "getGuestList", params={key: value, "pageSize": 25})
            rows = [g for g in (data.get("data") or []) if isinstance(g, dict)]
            if rows:
                return [self._to_guest(g) for g in rows]
        return []

    def get_guest(self, guest_id: str) -> Guest | None:
        data = self._call("GET", "getGuest", params={"guestID": guest_id})
        raw = data.get("data")
        return self._to_guest(raw) if isinstance(raw, dict) else None

    def list_room_types(self) -> list[RoomType]:
        data = self._call("GET", "getRoomTypes", params={"pageSize": 100})
        out = []
        for raw in data.get("data") or []:
            if not isinstance(raw, dict):
                continue
            out.append(RoomType(
                id=str(raw.get("roomTypeID") or ""), name=str(raw.get("roomTypeName") or ""),
                max_occupancy=int(raw.get("maxGuests") or 2),
                count=int(raw.get("roomTypeUnits") or 0), extra=raw))
        return out

    def get_availability(self, date_from: str, date_to: str) -> list[RateRow]:
        """``getAvailableRoomTypes`` — what can actually be sold in the window."""
        data = self._call("GET", "getAvailableRoomTypes",
                          params={"startDate": date_from, "endDate": date_to})
        out: list[RateRow] = []
        for prop in data.get("data") or []:
            for raw in (prop.get("propertyRooms") or []) if isinstance(prop, dict) else []:
                out.append(RateRow(
                    date=date_from, room_type_id=str(raw.get("roomTypeID") or ""),
                    price=float(raw.get("roomRate") or 0) or 0.0,
                    currency=self.settings.hotel.currency,
                    available=int(raw.get("roomsAvailable") or 0), extra=raw))
        return out

    def get_rates(self, date_from: str, date_to: str,
                  room_type: str | None = None) -> list[RateRow]:
        """Per-day rates from ``getRatePlans`` with ``detailedRates=true``."""
        data = self._call("GET", "getRatePlans", params={
            "startDate": date_from, "endDate": date_to, "detailedRates": "true"})
        out: list[RateRow] = []
        for plan in data.get("data") or []:
            if not isinstance(plan, dict):
                continue
            rtid = str(plan.get("roomTypeID") or "")
            if room_type is not None and rtid != room_type:
                continue
            for day in plan.get("roomRateDetailed") or []:
                out.append(RateRow(
                    date=str(day.get("date") or "")[:10], room_type_id=rtid,
                    price=float(day.get("rate") or 0) or 0.0,
                    currency=self.settings.hotel.currency,
                    min_los=int(day.get("minLos") or 1),
                    available=int(day.get("roomsAvailable") or 0),
                    closed=bool(day.get("closed")),
                    extra={"rateID": plan.get("rateID"), "ratePlanNamePublic":
                           plan.get("ratePlanNamePublic")}))
        return out

    def get_folio(self, reservation_id: str) -> dict:
        """Charges and payments on one reservation, as Cloudbeds returns them."""
        data = self._call("GET", "getReservation",
                          params={"reservationID": reservation_id}).get("data") or {}
        return {"reservation_id": str(reservation_id),
                "balance": float(data.get("balance") or 0) or 0.0,
                "total": float(data.get("total") or 0) or 0.0,
                "lines": data.get("rooms") or [], "raw": data}

    def list_housekeeping(self, date: str) -> list[dict]:
        data = self._call("GET", "getHousekeepingStatus", params={"date": date})
        return [r for r in (data.get("data") or []) if isinstance(r, dict)]

    # -- writes (guarded) --------------------------------------------------
    @guarded_write("pms_write")
    def add_note(self, reservation_id: str, text: str) -> dict:
        """``postReservationNote`` — the audit trail a hotel actually reads."""
        return self._call("POST", "postReservationNote", form={
            "reservationID": reservation_id, "reservationNote": text})

    @guarded_write("pms_write")
    def set_rate(self, date: str, room_type: str, price: float) -> dict:
        """``putRate`` for one date x room type.

        Cloudbeds needs the rate plan id, so we look it up for that day first.
        Pushing a rate for a day with no plan is refused rather than guessed.
        """
        rates = self.get_rates(date, date, room_type)
        rate_id = next((r.extra.get("rateID") for r in rates if r.extra.get("rateID")), None)
        if not rate_id:
            raise AdapterNotConfigured(
                f"pms_cloudbeds: no rate plan found for {room_type} on {date}; "
                "refusing to guess which plan to write.")
        return self._call("POST", "putRate", json_body={
            "rates": [{"rateID": rate_id,
                       "interval": [{"startDate": date, "endDate": date,
                                     "rate": round(float(price), 2)}]}]})

    @guarded_write("pms_write")
    def update_reservation(self, reservation_id: str, patch: dict) -> dict:
        """``putReservation`` with a flat patch of Cloudbeds field names."""
        form = {"reservationID": reservation_id}
        form.update({k: v for k, v in (patch or {}).items()})
        return self._call("PUT", "putReservation", form=form)

    @guarded_write("pms_write")
    def set_room_status(self, room: str, status: str) -> dict:
        """``postHousekeepingStatus`` — clean / dirty / inspected."""
        return self._call("POST", "postHousekeepingStatus",
                          form={"roomID": room, "roomCondition": status})
