"""core.adapters.base — the interfaces every system connector implements.

An adapter is the only code that knows about a specific vendor. Everything above
it works with the dataclasses in this module, so swapping a PMS means swapping
one file, not rewriting the agent.

Three honesty levels, reported by :meth:`Adapter.status`:

``built``      written against the real API and tested against it.
``universal``  works with any system through a common protocol (IMAP/SMTP, CSV
               import/export, an HTTP webhook).
``stub``       interface only. Calling it raises :class:`AdapterNotImplemented`
               with a recipe for asking your Claude session to write it.

Every adapter implements :meth:`ping` (is it reachable and configured?) and
:meth:`capabilities` (which methods actually do something), which is what
``make doctor`` and the README status tables read.

**Writes are guarded.** Every method that changes something outside this repo is
decorated with ``@guarded_write("<action>")``. The decorator calls
``core.review.assert_write_allowed`` before the body runs, so shadow mode,
``--dry-run`` and the approval rules are enforced in one place instead of being
re-implemented (and forgotten) in each adapter.
"""

from __future__ import annotations

import re

from pathlib import Path

import functools
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from core.config import AdapterConfig, Settings


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------
class AdapterError(RuntimeError):
    """Base class for adapter problems."""


class AdapterNotConfigured(AdapterError):
    """Credentials or settings are missing. The message says exactly which."""


class AdapterNotImplemented(NotImplementedError):
    """This system has an interface but no implementation yet.

    The message points at the recipe in ``docs/integrations.md`` so the hotel's
    Claude session can write the adapter for their own system.
    """

    def __init__(self, system: str, recipe_anchor: str = "implement-your-own",
                 method: str = "") -> None:
        where = f"{system}.{method}()" if method else system
        super().__init__(
            f"No adapter is implemented for {where} yet.\n"
            f"  This is a stub on purpose: we will not pretend an integration exists.\n"
            f"  To add it, open Claude Code in this folder and say:\n"
            f'    "Read docs/integrations.md#{recipe_anchor} and implement the {system} '
            f'adapter for our system."\n'
            f"  Then run `make doctor` to check it.")
        self.system, self.recipe_anchor, self.method = system, recipe_anchor, method


# --------------------------------------------------------------------------
# shared value types
# --------------------------------------------------------------------------
@dataclass
class HealthCheck:
    """Result of :meth:`Adapter.ping`."""

    ok: bool
    adapter: str = ""
    detail: str = ""
    fix_hint: str = ""


@dataclass
class Guest:
    """A person. ``id`` is the PMS guest id when there is one."""

    id: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    country: str = ""
    language: str = ""
    vip: bool = False
    notes: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()


@dataclass
class RoomType:
    """A bookable room category."""

    id: str = ""
    name: str = ""
    max_occupancy: int = 2
    count: int = 0
    rank: int = 0
    extra: dict = field(default_factory=dict)


@dataclass
class Reservation:
    """One booking. Dates are ISO ``YYYY-MM-DD``; money is a float in hotel currency."""

    id: str = ""
    external_ref: str = ""
    status: str = "confirmed"
    check_in: str = ""
    check_out: str = ""
    room_type_id: str = ""
    room_type_name: str = ""
    room_id: str = ""
    adults: int = 2
    children: int = 0
    source: str = ""
    total: float = 0.0
    balance: float = 0.0
    currency: str = "EUR"
    guest: Guest = field(default_factory=Guest)
    notes: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def nights(self) -> int:
        from datetime import date
        try:
            return max(0, (date.fromisoformat(self.check_out)
                           - date.fromisoformat(self.check_in)).days)
        except ValueError:
            return 0


@dataclass
class RateRow:
    """One date x room type price point."""

    date: str = ""
    room_type_id: str = ""
    price: float = 0.0
    currency: str = "EUR"
    min_los: int = 1
    available: int = 0
    closed: bool = False
    extra: dict = field(default_factory=dict)


@dataclass
class EmailMessage:
    """One email, already redacted on ingestion."""

    id: str = ""
    thread_id: str = ""
    message_id_header: str = ""
    references: str = ""
    from_email: str = ""
    from_name: str = ""
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    subject: str = ""
    body_text: str = ""
    body_html: str = ""
    received_at: str = ""
    folder: str = "INBOX"
    labels: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


@dataclass
class ChatMessage:
    """One WhatsApp/SMS/chat message."""

    id: str = ""
    chat_id: str = ""
    from_number: str = ""
    from_name: str = ""
    text: str = ""
    sent_at: str = ""
    direction: str = "in"
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# the write guard
# --------------------------------------------------------------------------
def guarded_write(action: str) -> Callable:
    """Decorator: refuse the call unless the current mode allows ``action``.

    The wrapped method may take an ``item=`` keyword (a :class:`core.store.Item`).
    When present, the decision can also refuse an item that is already sent.
    In ``mode: shadow`` nothing goes through, approved or not: approvals are
    recorded and cleared at go-live (``core.review.stale_backlog``).
    """
    def decorate(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(self: "Adapter", *args: Any, **kwargs: Any) -> Any:
            from core.review import assert_write_allowed  # local: avoids a cycle
            item = kwargs.pop("item", None)
            settings = getattr(self, "settings", None)
            if settings is None:
                raise AdapterNotConfigured(
                    f"{type(self).__name__} has no settings; cannot check write permission")
            assert_write_allowed(settings, action, item)
            return func(self, *args, **kwargs)
        wrapper.__guarded_action__ = action  # type: ignore[attr-defined]
        return wrapper
    return decorate


# --------------------------------------------------------------------------
# base adapter
# --------------------------------------------------------------------------
class Adapter:
    """Shared plumbing: settings, per-adapter config, status and capabilities."""

    #: one of "built", "universal", "stub"
    status: str = "stub"
    #: short vendor-facing name, used in doctor output
    name: str = "adapter"
    #: the system family this adapter serves ("pms", "email", ...)
    system: str = ""

    def __init__(self, settings: Settings, config: AdapterConfig | None = None) -> None:
        self.settings = settings
        self.config = config or AdapterConfig()

    # -- introspection ----------------------------------------------------
    def ping(self) -> HealthCheck:
        """Is this adapter configured and reachable? Never raises."""
        return HealthCheck(ok=False, adapter=self.name, detail="ping() not implemented")

    def capabilities(self) -> set[str]:
        """Names of the methods that actually do something on this adapter."""
        return set()

    def opt(self, key: str, default: Any = None, env: str | None = None) -> Any:
        """Read an option from ``systems.<x>`` in hotel.yaml, or from the environment."""
        import os
        if env and os.environ.get(env):
            return os.environ[env]
        return self.config.get(key, default)

    def require(self, *env_vars: str) -> None:
        """Raise :class:`AdapterNotConfigured` naming every missing variable."""
        import os
        missing = [v for v in env_vars if not os.environ.get(v)]
        if missing:
            raise AdapterNotConfigured(
                f"{self.name}: missing {', '.join(missing)}. Add them to .env "
                f"(see .env.example) and run `make doctor`.")


# --------------------------------------------------------------------------
# system interfaces
# --------------------------------------------------------------------------
class PMS(Adapter):
    """Property management system: reservations, guests, rooms, rates."""

    system = "pms"

    # -- reads ------------------------------------------------------------
    def list_reservations(self, date_from: str, date_to: str,
                          status: str | None = None) -> list[Reservation]:
        raise AdapterNotImplemented(self.name, method="list_reservations")

    def get_reservation(self, reservation_id: str) -> Reservation | None:
        raise AdapterNotImplemented(self.name, method="get_reservation")

    def find_guest(self, email: str = "", phone: str = "", name: str = "") -> list[Guest]:
        raise AdapterNotImplemented(self.name, method="find_guest")

    def get_guest(self, guest_id: str) -> Guest | None:
        raise AdapterNotImplemented(self.name, method="get_guest")

    def list_room_types(self) -> list[RoomType]:
        raise AdapterNotImplemented(self.name, method="list_room_types")

    def get_availability(self, date_from: str, date_to: str) -> list[RateRow]:
        raise AdapterNotImplemented(self.name, method="get_availability")

    def get_rates(self, date_from: str, date_to: str,
                  room_type: str | None = None) -> list[RateRow]:
        raise AdapterNotImplemented(self.name, method="get_rates")

    def list_arrivals(self, date: str) -> list[Reservation]:
        return [r for r in self.list_reservations(date, date) if r.check_in == date]

    def list_departures(self, date: str) -> list[Reservation]:
        return [r for r in self.list_reservations(date, date) if r.check_out == date]

    def list_in_house(self, date: str) -> list[Reservation]:
        return [r for r in self.list_reservations(date, date)
                if r.check_in <= date < r.check_out]

    def get_folio(self, reservation_id: str) -> dict:
        raise AdapterNotImplemented(self.name, method="get_folio")

    def list_housekeeping(self, date: str) -> list[dict]:
        raise AdapterNotImplemented(self.name, method="list_housekeeping")

    # -- writes (all guarded) ---------------------------------------------
    @guarded_write("pms_write")
    def set_rate(self, date: str, room_type: str, price: float) -> dict:
        raise AdapterNotImplemented(self.name, method="set_rate")

    @guarded_write("pms_write")
    def add_note(self, reservation_id: str, text: str) -> dict:
        raise AdapterNotImplemented(self.name, method="add_note")

    @guarded_write("pms_write")
    def update_reservation(self, reservation_id: str, patch: dict) -> dict:
        raise AdapterNotImplemented(self.name, method="update_reservation")

    @guarded_write("pms_write")
    def set_room_status(self, room: str, status: str) -> dict:
        raise AdapterNotImplemented(self.name, method="set_room_status")


class Email(Adapter):
    """A mailbox the agent reads from and replies into."""

    system = "email"

    def fetch_unread(self, since: str | None = None, folder: str = "INBOX",
                     limit: int = 50) -> list[EmailMessage]:
        raise AdapterNotImplemented(self.name, method="fetch_unread")

    def fetch_thread(self, thread_id: str) -> list[EmailMessage]:
        raise AdapterNotImplemented(self.name, method="fetch_thread")

    def signature(self) -> str:
        """The block appended to every outbound email, whatever the adapter.

        Read from ``systems.email.options.signature_file`` (default
        ``knowledge/signature.md``). This is where the hotel's sign-off and the
        EU AI Act Article 50 disclosure line live, so no adapter can forget them.
        """
        from core.config import repo_root
        rel = self.opt("signature_file", "") or "knowledge/signature.md"
        path = Path(rel)
        if not path.is_absolute():
            path = repo_root() / path
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8")
        # Strip YAML frontmatter (--- ... ---): it is metadata, not sign-off text.
        text = re.sub(r"\A\s*---\n.*?\n---\n", "", text, flags=re.S)
        return text.strip()

    def with_signature(self, body_md: str) -> str:
        """``body_md`` plus the signature block, unless it is already there."""
        sig = self.signature()
        if not sig or sig in body_md:
            return body_md
        return body_md.rstrip() + "\n\n" + sig

    @guarded_write("send_email")
    def send(self, to: str | list[str], subject: str, body_md: str,
             reply_to_message_id: str | None = None, cc: list[str] | None = None,
             attachments: list[str] | None = None) -> dict:
        raise AdapterNotImplemented(self.name, method="send")

    @guarded_write("email_write")
    def mark_read(self, message_id: str) -> dict:
        raise AdapterNotImplemented(self.name, method="mark_read")

    @guarded_write("email_write")
    def label(self, message_id: str, label: str) -> dict:
        raise AdapterNotImplemented(self.name, method="label")


class Messaging(Adapter):
    """WhatsApp / SMS / chat, on the hotel's own account."""

    system = "messaging"

    def fetch_new(self, since: str | None = None, limit: int = 50) -> list[ChatMessage]:
        raise AdapterNotImplemented(self.name, method="fetch_new")

    def disclosure(self) -> str:
        """The short AI-disclosure line appended to guest chat messages.

        Read from ``systems.messaging.options.disclosure_file`` (default
        ``knowledge/disclosure.md``). Chat is too short for an email signature,
        so this is one sentence - the EU AI Act Article 50 line for WhatsApp,
        web chat and SMS. Empty file or missing file -> nothing appended.
        """
        from core.config import repo_root
        rel = self.opt("disclosure_file", "") or "knowledge/disclosure.md"
        path = Path(rel)
        if not path.is_absolute():
            path = repo_root() / path
        if not path.exists():
            return ""
        text = re.sub(r"\A\s*---\n.*?\n---\n", "", path.read_text(encoding="utf-8"), flags=re.S)
        return " ".join(text.split())

    def with_disclosure(self, text: str, *, guest_facing: bool = True) -> str:
        """``text`` plus the disclosure line (once), for guest-facing sends.

        Pass ``guest_facing=False`` for a staff chat (a rota, a swap, a duty
        note): the Article 50 line is for guests, not colleagues.
        """
        if not guest_facing:
            return text
        line = self.disclosure()
        if not line or line in text:
            return text
        return text.rstrip() + "\n\n" + line

    @guarded_write("send_message")
    def send(self, chat_id: str, text: str, *, guest_facing: bool = True) -> dict:
        raise AdapterNotImplemented(self.name, method="send")

    @guarded_write("send_message")
    def notify_staff(self, text: str) -> dict:
        raise AdapterNotImplemented(self.name, method="notify_staff")


class Sheets(Adapter):
    """Where humans read the agent's output: a spreadsheet or a CSV file."""

    system = "sheets"

    def read(self, sheet: str) -> list[list[Any]]:
        raise AdapterNotImplemented(self.name, method="read")

    @guarded_write("sheets_write")
    def append(self, sheet: str, rows: Iterable[Iterable[Any]]) -> dict:
        raise AdapterNotImplemented(self.name, method="append")

    @guarded_write("sheets_write")
    def write(self, sheet: str, rows: Iterable[Iterable[Any]]) -> dict:
        raise AdapterNotImplemented(self.name, method="write")


# --------------------------------------------------------------------------
# stub families — interface now, implementation when a hotel needs it
# --------------------------------------------------------------------------
class _StubAdapter(Adapter):
    """Shared behaviour for the not-yet-built system families."""

    status = "stub"

    def ping(self) -> HealthCheck:
        return HealthCheck(
            ok=False, adapter=self.name,
            detail=f"{self.system} adapter is a stub (no implementation)",
            fix_hint=f"See docs/integrations.md#implement-your-own to add {self.system}.")

    def capabilities(self) -> set[str]:
        return set()

    def _nope(self, method: str) -> None:
        raise AdapterNotImplemented(self.system, method=method)


class POS(_StubAdapter):
    """Point of sale: covers, checks, voids, discounts."""

    system, name = "pos", "pos_stub"

    def list_checks(self, date: str) -> list[dict]:
        self._nope("list_checks")

    def list_voids(self, date: str) -> list[dict]:
        self._nope("list_voids")


class Accounting(_StubAdapter):
    """Ledger / bookkeeping system: invoices, payments, journals."""

    system, name = "accounting", "accounting_stub"

    def list_invoices(self, date_from: str, date_to: str) -> list[dict]:
        self._nope("list_invoices")

    @guarded_write("accounting_write")
    def push_invoice(self, invoice: dict) -> dict:
        self._nope("push_invoice")


class Reviews(_StubAdapter):
    """Guest review platforms (OTA, Google, TripAdvisor)."""

    system, name = "reviews", "reviews_stub"

    def list_reviews(self, since: str | None = None) -> list[dict]:
        self._nope("list_reviews")

    @guarded_write("publish")
    def reply(self, review_id: str, text: str) -> dict:
        self._nope("reply")


class Calendar(_StubAdapter):
    """Staff or event calendars."""

    system, name = "calendar", "calendar_stub"

    def list_events(self, date_from: str, date_to: str) -> list[dict]:
        self._nope("list_events")

    @guarded_write("calendar_write")
    def create_event(self, event: dict) -> dict:
        self._nope("create_event")


class Payments(_StubAdapter):
    """Card processing. Deliberately read-mostly: charging money needs a human."""

    system, name = "payments", "payments_stub"

    def list_charges(self, date_from: str, date_to: str) -> list[dict]:
        self._nope("list_charges")

    @guarded_write("payment")
    def refund(self, charge_id: str, amount: float) -> dict:
        self._nope("refund")


class Procurement(_StubAdapter):
    """Supplier catalogues and purchase orders."""

    system, name = "procurement", "procurement_stub"

    def list_suppliers(self) -> list[dict]:
        self._nope("list_suppliers")

    @guarded_write("procurement_write")
    def create_order(self, order: dict) -> dict:
        self._nope("create_order")


class Locks(_StubAdapter):
    """Door locks / key cards."""

    system, name = "locks", "locks_stub"

    def list_keys(self, reservation_id: str) -> list[dict]:
        self._nope("list_keys")

    @guarded_write("locks_write")
    def issue_key(self, reservation_id: str, valid_from: str, valid_to: str) -> dict:
        self._nope("issue_key")


class Courier(_StubAdapter):
    """Parcel shipping: labels and tracking (lost & found returns, gifts)."""

    system, name = "courier", "courier_stub"

    def track(self, tracking_id: str) -> dict:
        self._nope("track")

    @guarded_write("courier_write")
    def create_shipment(self, shipment: dict) -> dict:
        self._nope("create_shipment")
