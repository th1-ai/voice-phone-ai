#!/usr/bin/env python3
"""tools/demo.py - the whole loop on the bundled fixtures, zero credentials.

    make demo
    python3 tools/demo.py

Forces `llm.provider=mock` and `mode=shadow` regardless of config/hotel.yaml,
so this always works on a fresh clone with a blank .env (ARCHITECTURE.md
section 1, "works in 5 minutes with zero credentials"). It runs against its
own database (data/demo/demo.db) so running it twice always shows the same
sample transcripts, and never touches data/agent.db (that is `make run`'s
file).

Prints one line every check reads for the pass/fail signal:

    DEMO OK - 11 items processed, 9 drafted, 0 sent (shadow)
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_email, get_pms  # noqa: E402
from core.config import ConfigError, load_settings, sub_data_dir  # noqa: E402
from core.log import summary_line  # noqa: E402
from core.store import Store  # noqa: E402

import store_ext  # noqa: E402
from engine import process_call  # noqa: E402


def main() -> int:
    # load_settings(demo=True) forces provider=mock, mode=shadow AND every
    # systems.*.adapter to mock - whatever config/hotel.yaml says. See
    # factory/workflows/build-repo.md section 5.
    try:
        settings = load_settings(demo=True)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    demo_db = sub_data_dir("demo") / "demo.db"
    if demo_db.exists():
        demo_db.unlink()  # every `make demo` is a clean, repeatable run
    store = Store(settings, path=demo_db)
    store_ext.ensure_schema(store)

    email = get_email(settings)
    pms = get_pms(settings)
    calls = email.fetch_unread(limit=50)
    if not calls:
        print("no fixtures found in fixtures/inbound/ - nothing to demo", file=sys.stderr)
        return 1

    stats = {"processed": 0, "drafted": 0, "needs_human": 0, "sent": 0}
    print(f"Voice / Phone AI demo - {len(calls)} sample call transcript(s) from "
         f"fixtures/inbound/\n")
    for msg in calls:
        item, _ = process_call(settings, store, pms, msg, provider="mock")
        stats["processed"] += 1
        if item.review_status == "skipped":
            print(f"  {msg.id}: \"{msg.subject}\" -> call_type={item.intent} "
                 f"status=skipped (no action needed)")
            continue
        stats["drafted"] += 1
        if item.review_status == "needs_human":
            stats["needs_human"] += 1
        print(f"  {msg.id}: \"{msg.subject}\" -> call_type={item.intent} "
             f"confidence={item.confidence:.2f} status={item.review_status}")

    print(f"\n{stats['needs_human']} of {stats['drafted']} drafted callback(s) need a "
         f"person to look first - see docs/safety.md for what always does.")
    print("Nothing was sent: mode is shadow, and demo never calls send() at all.")
    print("Next: `make review` to see the drafts, or read workflows/10-calls.md.\n")

    print(f"DEMO OK - {summary_line(stats, settings.mode)}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
