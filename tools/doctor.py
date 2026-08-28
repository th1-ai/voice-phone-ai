#!/usr/bin/env python3
"""tools/doctor.py - is Voice / Phone AI configured and reachable right now?

    make doctor
    python3 tools/doctor.py

Runs the generic core.doctor checks (python, deps, config, .env, hotel
identity, mode, llm provider, every adapter, the store, knowledge) plus the
checks specific to this agent: rooms/restaurant/room-service configuration
and the two prompt tasks. Exits 0 when everything passed, 1 when a FAIL
line needs fixing. Never a traceback.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import ConfigError, Settings, load_settings  # noqa: E402
from core.doctor import Check, FAIL, PASS, WARN, print_table, run_checks  # noqa: E402


def check_calls_config(settings: Settings) -> list[Check]:
    out = []
    room_types = settings.agent_get("rooms.room_types", {})
    if not room_types:
        out.append(Check("room types", FAIL, "no rooms.room_types in config/agent.yaml",
                         "Copy config/agent.example.yaml to config/agent.yaml - it ships "
                         "with four sample room types."))
    else:
        out.append(Check("room types", PASS, f"{len(room_types)}: {', '.join(room_types)}"))

    restaurant = settings.agent_get("restaurant", {})
    out.append(Check("restaurant", PASS if restaurant.get("name") else WARN,
                     restaurant.get("name", "not configured - table bookings will always "
                                    "need a human")))

    menu = settings.agent_get("room_service.menu", [])
    out.append(Check("room-service menu", PASS if menu else WARN,
                     f"{len(menu)} item(s)" if menu else "no room_service.menu configured - "
                     "orders will always need a human"))
    return out


def check_prompts() -> Check:
    missing = [p for p in ("prompts/classify.md", "prompts/draft.md",
                           "prompts/schemas/classify.json", "prompts/schemas/draft.json")
              if not (REPO_ROOT / p).is_file()]
    if missing:
        return Check("prompts", FAIL, f"missing {', '.join(missing)}",
                     "These ship with the repo - restore them from git.")
    return Check("prompts", PASS, "classify.md + draft.md + schemas present")


def check_call_ingestion(settings: Settings) -> Check:
    """Voice / Phone AI has no telephony adapter - it reads call transcripts
    through systems.email.adapter. See docs/how-it-works.md."""
    adapter = settings.systems.email.adapter
    if adapter == "mock":
        # systems.email.fixtures_dir (config/hotel.yaml) can point the mock
        # adapter at a folder of your own sample call transcripts instead of
        # the shipped fixtures/inbound/ - say which one is actually being
        # read, since it is silently not the default once that is set.
        fixtures_dir = settings.systems.email.get("fixtures_dir") or "fixtures/inbound/"
        return Check("call transcript source", WARN,
                     f"systems.email.adapter is mock - reads {fixtures_dir} only",
                     "Point it at a mailbox that receives call-transcript emails from "
                     "your voicemail or call-recording system (systems.email.adapter: "
                     "imap or gmail) when you are ready - see docs/integrations.md. To "
                     "try your own sample calls first without touching fixtures/inbound/, "
                     "set systems.email.fixtures_dir instead.")
    return Check("call transcript source", PASS, f"reading call transcripts via {adapter}")


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        checks = run_checks(None) + [Check("config", FAIL, str(exc),
                                           "Fix config/hotel.yaml or config/agent.yaml.")]
        return print_table(checks, title="Voice / Phone AI - doctor")

    checks = run_checks(settings, extra=[check_calls_config])
    checks.append(check_prompts())
    checks.append(check_call_ingestion(settings))
    return print_table(checks, title="Voice / Phone AI - doctor")


if __name__ == "__main__":
    raise SystemExit(main())
