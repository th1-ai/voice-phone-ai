#!/usr/bin/env python3
"""tools/run.py - Voice / Phone AI's main loop: fetch -> classify -> preview -> draft -> queue.

    python3 tools/run.py --once
    python3 tools/run.py --watch
    python3 tools/run.py --once --dry-run
    python3 tools/run.py --once --limit 5
    python3 tools/run.py --once --provider mock

One pass: read unread call transcripts (`systems.email.adapter` - see
docs/how-it-works.md for why email is the ingestion channel), skip anything
already seen, classify each new one, preview the booking it implies (never
writes), draft a callback, and queue it in the review FSM (core.store).
Voice / Phone AI never sends or writes a booking on its own -
workflows/80-review.md and docs/safety.md cover the review queue and the
shadow/live switch.

Exit codes: 0 ok, 3 waiting on an `interactive` answer (see the message),
1 a real error.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_email, get_pms  # noqa: E402
from core.adapters.base import AdapterError  # noqa: E402
from core.config import ConfigError, load_settings  # noqa: E402
from core.llm import LLMError, LLMPendingInteractive  # noqa: E402
from core.log import Run, get_logger, summary_line  # noqa: E402
from core.store import Store, StoreError  # noqa: E402

import store_ext  # noqa: E402
from engine import process_call  # noqa: E402

log = get_logger("run")


def one_pass(settings, store, *, limit: int, provider: str | None) -> tuple[int, dict]:
    stats = {"processed": 0, "drafted": 0, "needs_human": 0, "sent": 0, "skipped": 0}
    with Run("calls", settings, store) as run:
        email = get_email(settings)
        pms = get_pms(settings)
        calls = email.fetch_unread(limit=limit)
        for msg in calls:
            try:
                item, did_work = process_call(settings, store, pms, msg, provider=provider)
            except LLMPendingInteractive as exc:
                run.stats = dict(stats)
                print(str(exc))
                return 3, stats
            if not did_work:
                stats["skipped"] += 1
                continue
            stats["processed"] += 1
            if item.review_status == "skipped":
                stats["skipped"] += 1
            else:
                stats["drafted"] += 1
                if item.review_status == "needs_human":
                    stats["needs_human"] += 1
            log.info("queued", item_id=item.id, call_type=item.intent,
                     status=item.review_status)
        reaped = store.reap_stuck_sending()
        if reaped:
            log.warn("reaped stuck sends", count=len(reaped))
        run.stats = dict(stats)
    return 0, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--once", action="store_true", help="run a single pass (default)")
    mode_group.add_argument("--watch", action="store_true",
                            help="keep running on the configured interval")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute everything, write nothing, even in live mode")
    parser.add_argument("--limit", type=int, default=20, help="max call transcripts per pass")
    parser.add_argument("--provider", default=None,
                        help="override llm.provider for this run")
    parser.add_argument("--poll-seconds", type=int, default=None,
                        help="override the --watch interval (default: agent.yaml or 600)")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(provider=args.provider, dry_run=args.dry_run)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    # --dry-run is a rehearsal: compute everything, write nothing - not even
    # to this repo's own data/agent.db. An in-memory database gives every
    # tool call somewhere real to write during the pass (so the code path is
    # exercised exactly as normal) while guaranteeing nothing lands on disk.
    store = Store(settings, path=":memory:" if settings.dry_run else None)
    store_ext.ensure_schema(store)
    try:
        if args.watch:
            poll_seconds = args.poll_seconds or int(settings.agent_get("poll_seconds", 600))
            while True:
                code, stats = one_pass(settings, store, limit=args.limit,
                                       provider=args.provider)
                print(summary_line(stats, settings.mode))
                if code != 0:
                    return code
                time.sleep(poll_seconds)
        code, stats = one_pass(settings, store, limit=args.limit, provider=args.provider)
        print(summary_line(stats, settings.mode))
        return code
    except AdapterError as exc:
        print(f"integration error: {exc}", file=sys.stderr)
        print("Run `make doctor` to see what is missing and how to fix it.", file=sys.stderr)
        return 1
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except StoreError as exc:
        print(f"cannot do that: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
