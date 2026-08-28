#!/usr/bin/env python3
"""tools/report.py - what the agent did, and what it cost.

    make report
    python3 tools/report.py
    python3 tools/report.py --json

Reads data/agent.db - nothing here calls a model or an adapter. Numbers, each
tied to a roster claim (see README.md section 2 and docs/benefits.md):

``volumes``            calls by call_type and by review_status right now.
``ledger``              how many room, table, room-service and guest-request
                        rows this agent has actually written (only ever
                        populated once a send has gone through - see
                        docs/how-it-works.md design decision 3).
``edit %``              of everything a human approved or edited, how often
                        they had to rewrite the callback rather than approve
                        it as-is.
``time-to-first-draft`` average minutes from a call transcript landing
                        (``payload.received_at``) to a callback being ready
                        for a human.
``spend``               LLM calls, tokens and cost, from ``core.llm``'s usage
                        logging (``core.store.usage_totals``).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import ConfigError, load_settings  # noqa: E402
from core.store import Store, StoreError  # noqa: E402

import store_ext  # noqa: E402


def volumes(store: Store) -> dict:
    by_status = store.counts()
    rows = store.db.execute("SELECT intent, COUNT(*) AS n FROM items GROUP BY intent").fetchall()
    by_call_type = {(r["intent"] or "unclassified"): r["n"] for r in rows}
    return {"by_call_type": by_call_type, "by_status": by_status, "total": sum(by_status.values())}


def ledger(store: Store) -> dict:
    return {
        "room_bookings": store_ext.counts_by_kind(store, "room_bookings"),
        "table_bookings": store_ext.counts_by_kind(store, "table_bookings"),
        "room_service_orders": store_ext.counts_by_kind(store, "room_service_orders"),
        "guest_requests": store_ext.counts_by_kind(store, "guest_requests"),
    }


def edit_stats(store: Store) -> dict:
    rows = store.db.execute(
        "SELECT item_id, action FROM events WHERE action IN "
        "('status:edited', 'status:approved')").fetchall()
    edited = {r["item_id"] for r in rows if r["action"] == "status:edited"}
    approved = {r["item_id"] for r in rows if r["action"] == "status:approved"} - edited
    total = len(edited) + len(approved)
    rate = (len(edited) / total) if total else 0.0
    return {"edited": len(edited), "approved_unchanged": len(approved), "rate": rate}


def time_to_first_draft_minutes(store: Store) -> dict:
    rows = store.db.execute(
        "SELECT id, payload_json, created_at FROM items WHERE kind='call'").fetchall()
    deltas: list[float] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            continue
        received = payload.get("received_at") or row["created_at"]
        ev = store.db.execute(
            "SELECT ts FROM events WHERE item_id=? AND action IN "
            "('status:pending_review', 'status:needs_human') ORDER BY ts ASC LIMIT 1",
            (row["id"],)).fetchone()
        if ev is None:
            continue
        try:
            start = datetime.fromisoformat(str(received)[:19])
            end = datetime.fromisoformat(str(ev["ts"])[:19])
        except ValueError:
            continue
        deltas.append(max(0.0, (end - start).total_seconds() / 60.0))
    avg = (sum(deltas) / len(deltas)) if deltas else 0.0
    return {"n": len(deltas), "avg_minutes": round(avg, 1)}


def spend(store: Store, since: str | None = None) -> dict:
    return store.usage_totals(since=since)


def build_report(store: Store, since: str | None = None) -> dict:
    return {"volumes": volumes(store), "ledger": ledger(store), "edits": edit_stats(store),
           "time_to_first_draft": time_to_first_draft_minutes(store),
           "spend": spend(store, since=since)}


def print_report(report: dict) -> None:
    v = report["volumes"]
    print("Voice / Phone AI - report\n")
    print(f"Calls: {v['total']} total")
    if v["by_call_type"]:
        print("  by call_type: " + ", ".join(f"{k}={n}" for k, n in
                                             sorted(v["by_call_type"].items())))
    if v["by_status"]:
        print("  by status:    " + ", ".join(f"{k}={n}" for k, n in
                                             sorted(v["by_status"].items())))

    led = report["ledger"]
    print("\nWritten so far (only after a send has gone through):")
    for table, counts in led.items():
        total = sum(counts.values())
        print(f"  {table}: {total} ({', '.join(f'{k}={n}' for k, n in sorted(counts.items())) or 'none yet'})")

    e = report["edits"]
    print(f"\nEdit rate: {e['edited']}/{e['edited'] + e['approved_unchanged']} approved "
         f"callback(s) needed a rewrite ({e['rate']*100:.0f}%).")

    t = report["time_to_first_draft"]
    if t["n"]:
        print(f"\nTime to first draft: {t['avg_minutes']} minute(s) average, over {t['n']} "
             f"call(s).")
        if t["avg_minutes"] > 1440:
            print("  (this looks unrealistically large - it usually means received_at on "
                 "one or more transcripts is not real wall-clock time, e.g. a fixture or "
                 "an older imported message. See docs/benefits.md before quoting this.)")
    else:
        print("\nTime to first draft: no completed calls yet.")

    s = report["spend"]
    print(f"\nSpend: {s['calls']} LLM call(s), {s['input_tokens']} input + "
         f"{s['output_tokens']} output token(s), USD {s['cost_usd']:.4f}.")
    if s["calls"] and s["cost_usd"] == 0.0:
        print("  (0.00 is expected on provider=mock, interactive or claude-code - only "
         "the anthropic provider bills per token.)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--since", default=None, help="ISO timestamp - only spend since then")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    store_ext.ensure_schema(store)
    try:
        report = build_report(store, since=args.since)
    except StoreError as exc:
        print(f"cannot do that: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
