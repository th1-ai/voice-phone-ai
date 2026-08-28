"""Concrete stub adapters for the system families nobody has wired up yet.

These exist so an agent can *ask* for a POS or an accounting system, get a real
object back, call ``ping()`` and ``capabilities()`` on it, and see honestly that
it is not implemented. Calling an actual method raises
:class:`~core.adapters.base.AdapterNotImplemented` with a recipe for adding it.

That is deliberate. A stub that quietly returns empty lists is worse than no
adapter at all: the agent looks like it works and silently does nothing.

To implement one, open Claude Code in this folder and say:

    "Read docs/integrations.md#implement-your-own and write the POS adapter for
     <our system>. Copy core/adapters/pms_csv.py as the shape."

Then register it in ``core/adapters/__init__.py`` and run ``make doctor``.
"""

from __future__ import annotations

from core.adapters.base import (Accounting, Calendar, Courier, Locks, Payments, POS,
                                Procurement, Reviews)


class PosStub(POS):
    """Point of sale: covers, checks, voids, discounts."""


class AccountingStub(Accounting):
    """Ledger / bookkeeping: invoices, payments, journals."""


class ReviewsStub(Reviews):
    """Guest review platforms: OTA, Google, TripAdvisor."""


class CalendarStub(Calendar):
    """Staff rotas and event calendars."""


class PaymentsStub(Payments):
    """Card processing. Read-mostly on purpose: moving money needs a person."""


class LocksStub(Locks):
    """Door locks and key cards."""


class ProcurementStub(Procurement):
    """Supplier catalogues and purchase orders."""


class CourierStub(Courier):
    """Parcel shipping: labels and tracking. Nothing ships without a person."""


STUBS = {
    "pos": PosStub, "accounting": AccountingStub, "reviews": ReviewsStub,
    "calendar": CalendarStub, "payments": PaymentsStub, "locks": LocksStub,
    "procurement": ProcurementStub, "courier": CourierStub,
}
