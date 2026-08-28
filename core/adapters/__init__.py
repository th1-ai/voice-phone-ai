"""core.adapters — the registry that turns config into a live connector.

``config/hotel.yaml`` says *which* adapter to use::

    systems:
      pms:       { adapter: csv }
      email:     { adapter: imap, mailbox: reservations@example.com }
      messaging: { adapter: mock }
      sheets:    { adapter: csv }

and this module hands the agent the object::

    from core.adapters import get_pms, get_email
    pms = get_pms(settings)
    for reservation in pms.list_arrivals("2026-09-01"):
        ...

Adapters are imported lazily, so a repo with no Google libraries installed can
still use the IMAP mailbox, and a bad name fails with a list of the valid ones
rather than an import error three files away.

**Adding your own.** Write the module next to these, then add one line to the
matching table below. ``make doctor`` picks it up automatically.
"""

from __future__ import annotations

import importlib
from typing import Any

from core.adapters.base import (Accounting, Adapter, AdapterError, AdapterNotConfigured,
                                AdapterNotImplemented, Calendar, ChatMessage, Email,
                                EmailMessage, Guest, HealthCheck, Locks, Messaging,
                                Payments, PMS, POS, Procurement, RateRow, Reservation,
                                Reviews, RoomType, Sheets, guarded_write)
from core.config import AdapterConfig, Settings

__all__ = [
    "get_pms", "get_email", "get_messaging", "get_sheets", "get_stub", "get_all",
    "sample_data_warning", "is_sample_source",
    "available", "Adapter", "PMS", "Email", "Messaging", "Sheets", "HealthCheck",
    "Reservation", "Guest", "RoomType", "RateRow", "EmailMessage", "ChatMessage",
    "AdapterError", "AdapterNotConfigured", "AdapterNotImplemented", "guarded_write",
]

#: ``{system: {name: "module:ClassName"}}`` — the whole registry.
REGISTRY: dict[str, dict[str, str]] = {
    "pms": {
        "mock": "core.adapters.pms_mock:MockPMS",
        "csv": "core.adapters.pms_csv:CsvPMS",
        "cloudbeds": "core.adapters.pms_cloudbeds:CloudbedsPMS",
        "cli": "core.adapters.pms_cli:CliPMS",
    },
    "email": {
        "mock": "core.adapters.email_mock:MockEmail",
        "imap": "core.adapters.email_imap:ImapEmail",
        "gmail": "core.adapters.email_gmail:GmailEmail",
    },
    "messaging": {
        "mock": "core.adapters.messaging_mock:MockMessaging",
        "unipile": "core.adapters.messaging_unipile:UnipileMessaging",
        "webhook": "core.adapters.messaging_webhook:WebhookMessaging",
    },
    "sheets": {
        "csv": "core.adapters.sheets_csv:CsvSheets",
        "mock": "core.adapters.sheets_csv:CsvSheets",
        "google": "core.adapters.sheets_google:GoogleSheets",
    },
}

#: system families that only have stubs so far
STUB_SYSTEMS = ("pos", "accounting", "reviews", "calendar", "payments", "procurement",
                "locks", "courier")


def available(system: str) -> list[str]:
    """Adapter names configured for one system family."""
    return sorted(REGISTRY.get(system, {}))


def _build(system: str, settings: Settings, config: AdapterConfig) -> Adapter:
    table = REGISTRY.get(system)
    if table is None:
        raise AdapterError(f"unknown system '{system}'. Known: {', '.join(REGISTRY)}")
    target = table.get(config.adapter)
    if target is None:
        raise AdapterNotConfigured(
            f"systems.{system}.adapter is '{config.adapter}', which does not exist.\n"
            f"  Available: {', '.join(available(system))}.\n"
            f"  Edit config/hotel.yaml, or write your own adapter — see "
            f"docs/integrations.md#implement-your-own.")
    module_name, _, class_name = target.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, class_name)(settings, config)


def get_pms(settings: Settings) -> PMS:
    """The property management system named in ``systems.pms.adapter``."""
    return _build("pms", settings, settings.systems.pms)  # type: ignore[return-value]


def get_email(settings: Settings) -> Email:
    """The mailbox named in ``systems.email.adapter``."""
    return _build("email", settings, settings.systems.email)  # type: ignore[return-value]


def get_messaging(settings: Settings) -> Messaging:
    """The chat channel named in ``systems.messaging.adapter``."""
    return _build("messaging", settings,
                  settings.systems.messaging)  # type: ignore[return-value]


def get_sheets(settings: Settings) -> Sheets:
    """The reporting target named in ``systems.sheets.adapter``."""
    return _build("sheets", settings, settings.systems.sheets)  # type: ignore[return-value]


def get_stub(system: str, settings: Settings) -> Adapter:
    """A stub adapter for a system family nobody has implemented yet.

    Returns a real object whose ``ping()`` says "stub" and whose methods raise
    :class:`AdapterNotImplemented` with the recipe. Never pretends to work.
    """
    from core.adapters.domain_stub import STUBS
    cls = STUBS.get(system)
    if cls is None:
        raise AdapterError(
            f"no stub for '{system}'. Stub families: {', '.join(STUB_SYSTEMS)}.")
    return cls(settings, AdapterConfig(adapter="stub"))


SOURCE_TO_SYSTEM = {"email": "email", "pms": "pms", "messaging": "messaging",
                    "whatsapp": "messaging", "chat": "messaging", "sms": "messaging"}


def _systems_used(settings: Settings) -> tuple[str, ...]:
    """Which of pms/email/messaging this agent actually reads from.

    `config/agent.yaml: systems_used: [messaging, sheets]` lets an agent that
    never touches the PMS keep `pms.adapter: mock` without a misleading
    sample-data warning. Absent -> all three are assumed in use.
    """
    declared = settings.agent_get("systems_used", None) if hasattr(settings, "agent_get") else None
    if isinstance(declared, (list, tuple)):
        # An explicit empty list means "this agent reads none of them" (a
        # reviews- or CSV-only agent); only an ABSENT key means "assume all".
        return tuple(str(x) for x in declared if str(x) in ("pms", "email", "messaging"))
    return ("pms", "email", "messaging")


def sample_data_warning(settings: Settings) -> str | None:
    """A one-line warning when a REAL run would read shipped fixtures.

    ``mock`` adapters are for `make demo` and tests. If a hotel runs the real
    loop before connecting anything, every item it sees is invented sample
    data - it must never look like the property's own. Returns None in demo.
    """
    if getattr(settings, "demo", False):
        return None
    mocks = [name for name in _systems_used(settings)
             if getattr(getattr(settings.systems, name, None), "adapter", "") == "mock"]
    if not mocks:
        return None
    return ("SAMPLE DATA: systems." + ", systems.".join(f"{m}.adapter is mock" for m in mocks)
            + " - this pass reads the shipped fixtures, not your property. Items are tagged"
              " [SAMPLE]; connect your systems in config/hotel.yaml (see docs/integrations.md).")


def is_sample_source(settings: Settings, source: str) -> bool:
    """True when an item created now is (at least partly) sample-derived.

    Outside demo, if ANY system this agent uses is still on the ``mock``
    adapter, every item the run produces rests on shipped fixtures - whatever
    the item's own ``source`` string is (agents use their own names: "call",
    "disputes", "weekly-rota"...). Conservative on purpose: a hotel must never
    mistake fixture-fed output for its own.
    """
    if getattr(settings, "demo", False):
        return False
    return sample_data_warning(settings) is not None


def get_all(settings: Settings) -> dict[str, Adapter]:
    """Every configured adapter, for ``make doctor``. Failures become error stubs."""
    out: dict[str, Adapter] = {}
    for system, getter in (("pms", get_pms), ("email", get_email),
                           ("messaging", get_messaging), ("sheets", get_sheets)):
        try:
            out[system] = getter(settings)  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 - doctor must always print a table
            out[system] = _BrokenAdapter(settings, system, str(exc))
    return out


class _BrokenAdapter(Adapter):
    """Stands in for an adapter that could not even be constructed."""

    status = "stub"

    def __init__(self, settings: Settings, system: str, error: str) -> None:
        super().__init__(settings, AdapterConfig(adapter="broken"))
        self.system, self.name, self.error = system, f"{system}_error", error

    def ping(self) -> HealthCheck:
        return HealthCheck(False, self.name, self.error.splitlines()[0][:160],
                           "Fix systems.%s in config/hotel.yaml." % self.system)

    def capabilities(self) -> set[str]:
        return set()
