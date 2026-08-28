"""core.store — SQLite state for the agent (``data/agent.db``).

One database, six tables:

``items``      the universal work queue: one row per inbound thing the agent
               handles (an email, a message, a reservation, an invoice).
``tasks``      tickler-style follow-ups hanging off an item (chase a supplier,
               re-ask a guest) with ``next_action_due`` and a follow-up cap.
``runs``       one row per ``tools/run.py`` pass, with provider and stats.
``events``     append-only audit trail: who (human | agent) did what, when.
``learnings``  before/after pairs harvested from human edits, for the coach.
``kv``         cursors and small scalars (last mail uid, last sync time).
``sequences``  transactional counters (invoice numbers) — never bumped on a
               dry run, so a rehearsal cannot burn a number.

The ``review_status`` state machine is enforced here and nowhere else::

    new ──> dispatched ──> auto_sent                    (terminal)
     │           │
     │           ├──> pending_review ─┐
     │           └──> needs_human ────┤──> approved ─┐
     ├──> pending_review ─────────────┤    edited ───┤──> sending ──> sent
     ├──> needs_human ────────────────┤    rejected  │                 │
     └──> skipped (terminal)          └──> stale     └──> failed <─────┘
                                                          └──> approved (human retry)

``tools/review.py`` is the only writer of ``approved | edited | rejected``;
``tools/run.py`` is the only writer of ``sending | sent``. The claim from
``approved|edited`` to ``sending`` is a single conditional UPDATE, so two
runners racing on the same queue can never both send the same item.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from core.config import Settings, data_dir

# --------------------------------------------------------------------------
# the state machine
# --------------------------------------------------------------------------
STATUSES = (
    "new", "dispatched", "pending_review", "needs_human", "auto_sent",
    "approved", "edited", "sending", "sent", "rejected", "failed",
    "skipped", "stale",
)

TERMINAL = frozenset({"auto_sent", "sent", "rejected", "skipped"})

#: to-states allowed from each from-state. Anything not listed raises.
TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset({"dispatched", "pending_review", "needs_human", "skipped"}),
    "dispatched": frozenset({"auto_sent", "pending_review", "needs_human", "failed"}),
    "pending_review": frozenset({"approved", "edited", "rejected", "stale"}),
    "needs_human": frozenset({"approved", "edited", "rejected", "stale"}),
    # a human may still change their mind before the send is claimed
    "approved": frozenset({"sending", "stale", "rejected"}),
    "edited": frozenset({"sending", "stale", "rejected"}),
    # "approved": a guard (shadow mode) blocked the send — the approval stands.
    "sending": frozenset({"sent", "failed", "approved"}),
    # failed is terminal-until-human: only a person may queue a retry.
    "failed": frozenset({"approved"}),
    # stale is a side state; a human can revive or discard it.
    "stale": frozenset({"pending_review", "rejected"}),
    "auto_sent": frozenset(),
    "sent": frozenset(),
    "rejected": frozenset(),
    "skipped": frozenset(),
}

SEND_QUEUE = ("approved", "edited")


class StoreError(RuntimeError):
    """Base class for store problems."""


class IllegalTransition(StoreError):
    """Raised when code asks for a ``review_status`` move the FSM forbids."""

    def __init__(self, item_id: str, current: str, to: str) -> None:
        allowed = ", ".join(sorted(TRANSITIONS.get(current, frozenset()))) or "(terminal)"
        super().__init__(
            f"item {item_id}: cannot move '{current}' -> '{to}'. Allowed from "
            f"'{current}': {allowed}."
        )
        self.item_id, self.current, self.to = item_id, current, to


def utcnow() -> str:
    """Current UTC time as an ISO-8601 string (the only timestamp format used)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _unjson(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------
# rows
# --------------------------------------------------------------------------
@dataclass
class Item:
    """One row of ``items`` — the unit of work the whole agent moves around."""

    id: str
    kind: str
    source: str
    external_id: str
    payload: dict = field(default_factory=dict)
    intent: str | None = None
    confidence: float | None = None
    draft: dict | None = None
    review_status: str = "new"
    assigned_to: str | None = None
    created_at: str = ""
    updated_at: str = ""
    sent_at: str | None = None
    sent_message_id: str | None = None
    error: str | None = None
    unique_key: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Item":
        return cls(
            id=row["id"], kind=row["kind"], source=row["source"],
            external_id=row["external_id"], payload=_unjson(row["payload_json"]) or {},
            intent=row["intent"], confidence=row["confidence"],
            draft=_unjson(row["draft_json"]), review_status=row["review_status"],
            assigned_to=row["assigned_to"], created_at=row["created_at"],
            updated_at=row["updated_at"], sent_at=row["sent_at"],
            sent_message_id=row["sent_message_id"], error=row["error"],
            unique_key=row["unique_key"],
        )

    def as_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "source": self.source,
            "external_id": self.external_id, "payload": self.payload,
            "intent": self.intent, "confidence": self.confidence, "draft": self.draft,
            "review_status": self.review_status, "assigned_to": self.assigned_to,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "sent_at": self.sent_at, "sent_message_id": self.sent_message_id,
            "error": self.error,
        }

    @property
    def is_sample(self) -> bool:
        """True for items read through a mock adapter outside `make demo`
        (core tags them ``_sample``); review tools print a [SAMPLE] marker."""
        return bool((self.payload or {}).get("_sample"))

@dataclass
class Task:
    """A tickler row: 'chase this again on <next_action_due>, at most N times'."""

    id: str
    kind: str
    ref_id: str
    status: str = "open"
    next_action_due: str | None = None
    follow_up_count: int = 0
    max_follow_ups: int = 3
    history: list = field(default_factory=list)
    payload: dict = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        return cls(
            id=row["id"], kind=row["kind"], ref_id=row["ref_id"], status=row["status"],
            next_action_due=row["next_action_due"], follow_up_count=row["follow_up_count"],
            max_follow_ups=row["max_follow_ups"], history=_unjson(row["history_json"]) or [],
            payload=_unjson(row["payload_json"]) or {},
        )


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,
  source        TEXT NOT NULL,
  external_id   TEXT NOT NULL,
  payload_json  TEXT,
  intent        TEXT,
  confidence    REAL,
  draft_json    TEXT,
  review_status TEXT NOT NULL DEFAULT 'new',
  assigned_to   TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  sent_at       TEXT,
  sent_message_id TEXT,
  error         TEXT,
  unique_key    TEXT,
  UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items (review_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_kind ON items (kind, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_unique_key
  ON items (kind, unique_key) WHERE unique_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS tasks (
  id               TEXT PRIMARY KEY,
  kind             TEXT NOT NULL,
  ref_id           TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'open',
  next_action_due  TEXT,
  follow_up_count  INTEGER NOT NULL DEFAULT 0,
  max_follow_ups   INTEGER NOT NULL DEFAULT 3,
  history_json     TEXT,
  payload_json     TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  UNIQUE (kind, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks (status, next_action_due);

CREATE TABLE IF NOT EXISTS runs (
  id          TEXT PRIMARY KEY,
  workflow    TEXT NOT NULL,
  provider    TEXT,
  mode        TEXT,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  stats_json  TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id     TEXT,
  run_id      TEXT,
  ts          TEXT NOT NULL,
  actor       TEXT NOT NULL,
  action      TEXT NOT NULL,
  detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_item ON events (item_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_action ON events (action, ts);

CREATE TABLE IF NOT EXISTS learnings (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  source_item TEXT,
  before      TEXT,
  after       TEXT,
  lesson      TEXT,
  applied_to  TEXT
);

CREATE TABLE IF NOT EXISTS kv (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS sequences (
  name  TEXT PRIMARY KEY,
  value INTEGER NOT NULL DEFAULT 0
);
"""


class Store:
    """SQLite-backed state. Cheap to construct; safe to construct per tool run."""

    def __init__(self, settings: Settings | None = None, path: Path | str | None = None) -> None:
        self.settings = settings
        if path is None:
            path = settings.db_path() if settings is not None else data_dir() / "agent.db"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), isolation_level=None, timeout=30.0)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self._supports_returning = sqlite3.sqlite_version_info >= (3, 35, 0)

    # -- agent schema extensions -------------------------------------------
    def migrate(self, sql: str) -> None:
        """Run an agent's own schema script (``CREATE TABLE IF NOT EXISTS ...``).

        Agents keep their domain tables (bookings, claims, invoices ...) beside
        the core ones in the same database. The script runs on every Store
        construction, so it must be idempotent: ``IF NOT EXISTS`` everywhere,
        no data statements. Call it once, right after ``Store(settings)``.
        """
        self.db.executescript(sql)

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- items ------------------------------------------------------------
    def upsert_item(self, source: str, external_id: str, *, kind: str = "message",
                    payload: dict | None = None, intent: str | None = None,
                    confidence: float | None = None, unique_key: str | None = None,
                    assigned_to: str | None = None) -> Item:
        """Insert-or-return by ``(source, external_id)``. Never double-processes.

        An existing row is returned untouched apart from ``payload`` (refreshed)
        so a re-run of the same inbox pass is a no-op for the state machine.
        """
        now = utcnow()
        # Items read through a mock adapter outside `make demo` are sample data
        # and carry a marker the review tools show as [SAMPLE].
        if payload is not None and self.settings is not None:
            try:
                from core.adapters import is_sample_source
                if is_sample_source(self.settings, source):
                    payload = {**payload, "_sample": True}
            except Exception:
                pass
        existing = self.get_by_external(source, external_id)
        if existing is not None:
            if payload is not None:
                # Underscore-prefixed keys are the agent's own stage caches
                # (e.g. ``_classify_cache``); a payload refresh from the source
                # system must never wipe them, or a retry after an interactive
                # pause loses its place. New keys always win.
                merged = dict(payload)
                for key, value in (existing.payload or {}).items():
                    if key.startswith("_") and key not in merged:
                        merged[key] = value
                if merged != existing.payload:
                    self.db.execute(
                        "UPDATE items SET payload_json=?, updated_at=? WHERE id=?",
                        (_json(merged), now, existing.id))
                    existing.payload = merged
            return existing
        item_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO items (id, kind, source, external_id, payload_json, intent, "
            "confidence, review_status, assigned_to, created_at, updated_at, unique_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, kind, source, external_id, _json(payload or {}), intent,
             confidence, "new", assigned_to, now, now, unique_key))
        self.record_event(item_id, "agent", "ingested", {"source": source, "kind": kind})
        item = self.get_item(item_id)
        assert item is not None
        return item

    def upsert_unique(self, kind: str, unique_key: str, payload: dict | None = None,
                      *, source: str | None = None) -> tuple[Item, bool]:
        """Ledger-style dedup: one row per ``(kind, unique_key)``.

        Use for "did this campaign already touch this reservation?" questions.
        Returns ``(item, created)``.
        """
        row = self.db.execute(
            "SELECT * FROM items WHERE kind=? AND unique_key=?", (kind, unique_key)).fetchone()
        if row is not None:
            return Item.from_row(row), False
        item = self.upsert_item(source or kind, unique_key, kind=kind,
                                payload=payload, unique_key=unique_key)
        return item, True

    def get_item(self, item_id: str) -> Item | None:
        row = self.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return Item.from_row(row) if row else None

    def get_by_external(self, source: str, external_id: str) -> Item | None:
        row = self.db.execute(
            "SELECT * FROM items WHERE source=? AND external_id=?",
            (source, external_id)).fetchone()
        return Item.from_row(row) if row else None

    def list_items(self, *, status: str | Iterable[str] | None = None, kind: str | None = None,
                   limit: int = 50, order: str = "created_at ASC") -> list[Item]:
        """List items, oldest first by default. ``status`` may be one value or many."""
        where, params = [], []
        if status:
            values = [status] if isinstance(status, str) else list(status)
            where.append(f"review_status IN ({','.join('?' * len(values))})")
            params += values
        if kind:
            where.append("kind=?")
            params.append(kind)
        sql = "SELECT * FROM items"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order} LIMIT ?"
        params.append(int(limit))
        return [Item.from_row(r) for r in self.db.execute(sql, params).fetchall()]

    def already_processed(self, source: str, external_ids: Iterable[str]) -> set[str]:
        """Row-level dedup on top of cursors.

        Mail cursors are day-granular, so a poll re-sees yesterday's messages.
        Feed the ids you just fetched and skip whatever comes back.

        A row still in ``new`` is NOT reported: that is an item the agent
        created and then parked (the ``interactive`` provider pended a prompt,
        a crash mid-pass). It must be picked up again on the next pass, not
        skipped forever. Every agent moves an item out of ``new`` as soon as it
        has finished with it (pending_review / needs_human / skipped ...).
        """
        ids = [str(x) for x in external_ids]
        found: set[str] = set()
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            rows = self.db.execute(
                f"SELECT external_id FROM items WHERE source=? AND review_status != 'new' "
                f"AND external_id IN ({','.join('?' * len(chunk))})", [source, *chunk]).fetchall()
            found.update(r["external_id"] for r in rows)
        return found

    # -- state machine ----------------------------------------------------
    def transition(self, item_id: str, to: str, actor: str = "agent",
                   detail: dict | None = None) -> Item:
        """Move an item to ``to``. Raises :class:`IllegalTransition` on a bad move.

        ``actor`` is ``"human"`` or ``"agent"`` and is recorded on the event.
        """
        if to not in STATUSES:
            raise StoreError(f"unknown review_status '{to}' (known: {', '.join(STATUSES)})")
        item = self.get_item(item_id)
        if item is None:
            raise StoreError(f"no item {item_id}")
        if item.review_status == to:
            return item
        if to not in TRANSITIONS.get(item.review_status, frozenset()):
            raise IllegalTransition(item_id, item.review_status, to)
        self.db.execute("UPDATE items SET review_status=?, updated_at=? WHERE id=?",
                        (to, utcnow(), item_id))
        self.record_event(item_id, actor, f"status:{to}",
                          {"from": item.review_status, **(detail or {})})
        updated = self.get_item(item_id)
        assert updated is not None
        return updated

    def set_fields(self, item_id: str, **fields: Any) -> Item | None:
        """Update payload/intent/confidence/draft/assigned_to/error on an item."""
        cols, params = [], []
        mapping = {"payload": "payload_json", "draft": "draft_json"}
        for key, value in fields.items():
            col = mapping.get(key, key)
            if col not in ("payload_json", "draft_json", "intent", "confidence",
                           "assigned_to", "error", "sent_message_id", "unique_key"):
                raise StoreError(f"set_fields cannot write '{key}'")
            cols.append(f"{col}=?")
            params.append(_json(value) if col.endswith("_json") else value)
        if not cols:
            return self.get_item(item_id)
        params += [utcnow(), item_id]
        self.db.execute(f"UPDATE items SET {', '.join(cols)}, updated_at=? WHERE id=?", params)
        return self.get_item(item_id)

    # -- send queue -------------------------------------------------------
    def claim_for_send(self, limit: int = 5) -> list[Item]:
        """Atomically claim approved/edited items and flip them to ``sending``.

        One conditional UPDATE per row, so a second runner that reads the same
        candidates loses the race and gets nothing back. Always pair with
        :meth:`mark_sent` / :meth:`mark_send_failed`, and run
        :meth:`reap_stuck_sending` on every pass in case a process died between.
        """
        candidates = self.db.execute(
            "SELECT id FROM items WHERE review_status IN (?,?) ORDER BY updated_at ASC LIMIT ?",
            (*SEND_QUEUE, int(limit))).fetchall()
        claimed: list[Item] = []
        for row in candidates:
            item_id = row["id"]
            sql = ("UPDATE items SET review_status='sending', updated_at=? "
                   "WHERE id=? AND review_status IN (?,?)")
            params = (utcnow(), item_id, *SEND_QUEUE)
            if self._supports_returning:
                got = self.db.execute(sql + " RETURNING id", params).fetchall()
                won = bool(got)
            else:  # pragma: no cover - SQLite < 3.35
                cur = self.db.execute(sql, params)
                won = cur.rowcount == 1
            if won:
                self.record_event(item_id, "agent", "status:sending", {"claim": True})
                item = self.get_item(item_id)
                if item is not None:
                    claimed.append(item)
        return claimed

    def mark_sent(self, item_id: str, message_id: str | None = None) -> Item:
        """Record the provider's message id, THEN flip to ``sent`` (never the reverse)."""
        self.db.execute("UPDATE items SET sent_message_id=?, sent_at=? WHERE id=?",
                        (message_id, utcnow(), item_id))
        return self.transition(item_id, "sent", "agent", {"message_id": message_id})

    def mark_send_failed(self, item_id: str, error: str) -> Item:
        self.db.execute("UPDATE items SET error=? WHERE id=?", ((error or "")[:1000], item_id))
        return self.transition(item_id, "failed", "agent", {"error": (error or "")[:300]})

    def reap_stuck_sending(self, max_age_minutes: int = 30) -> list[str]:
        """Rescue rows stranded in ``sending`` by a crash between claim and send.

        Moves anything older than ``max_age_minutes`` to ``failed`` so a human
        sees it in the queue instead of it silently disappearing. Returns the ids.
        """
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(minutes=max_age_minutes)).isoformat(timespec="seconds")
        rows = self.db.execute(
            "SELECT id FROM items WHERE review_status='sending' AND updated_at < ?",
            (cutoff,)).fetchall()
        reaped = []
        for row in rows:
            self.mark_send_failed(row["id"], "reaper: stuck in sending")
            reaped.append(row["id"])
        return reaped

    def mark_stale(self, older_than_hours: int = 72, *,
                   statuses: tuple[str, ...] = ("pending_review", "needs_human"),
                   actor: str = "agent", reason: str = "") -> list[str]:
        """Flag review rows nobody has touched. Returns the ids moved to ``stale``.

        ``statuses`` may include ``approved``/``edited``: that is how the
        go-live step clears everything approved during shadow mode, which was
        recorded but never sent and is probably out of date by then.
        """
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=older_than_hours)).isoformat(timespec="seconds")
        marks = ",".join("?" * len(statuses))
        rows = self.db.execute(
            f"SELECT id FROM items WHERE review_status IN ({marks}) AND updated_at <= ?",
            [*statuses, cutoff]).fetchall()
        out = []
        for row in rows:
            self.transition(row["id"], "stale", actor,
                            {"older_than_hours": older_than_hours, "reason": reason})
            out.append(row["id"])
        return out

    # -- tasks (ticklers) -------------------------------------------------
    def upsert_task(self, kind: str, ref_id: str, *, next_action_due: str | None = None,
                    max_follow_ups: int = 3, payload: dict | None = None) -> Task:
        """Create or fetch the follow-up task for ``(kind, ref_id)``."""
        row = self.db.execute("SELECT * FROM tasks WHERE kind=? AND ref_id=?",
                              (kind, ref_id)).fetchone()
        if row is not None:
            return Task.from_row(row)
        now = utcnow()
        task_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO tasks (id, kind, ref_id, status, next_action_due, follow_up_count, "
            "max_follow_ups, history_json, payload_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, kind, ref_id, "open", next_action_due, 0, int(max_follow_ups),
             _json([]), _json(payload or {}), now, now))
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return Task.from_row(row)

    def due_tasks(self, kind: str | None = None, *, now: str | None = None,
                  limit: int = 50) -> list[Task]:
        """Open tasks whose ``next_action_due`` has passed. Act once per run."""
        now = now or utcnow()
        sql = ("SELECT * FROM tasks WHERE status='open' AND (next_action_due IS NULL "
               "OR next_action_due <= ?)")
        params: list[Any] = [now]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY next_action_due ASC LIMIT ?"
        params.append(int(limit))
        return [Task.from_row(r) for r in self.db.execute(sql, params).fetchall()]

    def advance_task(self, task_id: str, *, gap_days: int = 2, note: str = "") -> Task:
        """Record one follow-up and push ``next_action_due`` out by ``gap_days``.

        When ``follow_up_count`` reaches ``max_follow_ups`` the task flips to
        ``escalated`` so a human picks it up instead of the agent chasing forever.
        """
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise StoreError(f"no task {task_id}")
        task = Task.from_row(row)
        task.follow_up_count += 1
        task.history.append({"ts": utcnow(), "note": note})
        due = (datetime.now(timezone.utc) + timedelta(days=gap_days)).isoformat(timespec="seconds")
        status = "escalated" if task.follow_up_count >= task.max_follow_ups else "open"
        self.db.execute(
            "UPDATE tasks SET follow_up_count=?, history_json=?, next_action_due=?, "
            "status=?, updated_at=? WHERE id=?",
            (task.follow_up_count, _json(task.history), due, status, utcnow(), task_id))
        task.next_action_due, task.status = due, status
        return task

    def close_task(self, task_id: str, status: str = "done") -> None:
        self.db.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                        (status, utcnow(), task_id))

    # -- runs / events / learnings ---------------------------------------
    def start_run(self, workflow: str, provider: str = "", mode: str = "") -> str:
        run_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO runs (id, workflow, provider, mode, started_at) VALUES (?,?,?,?,?)",
            (run_id, workflow, provider, mode, utcnow()))
        return run_id

    def finish_run(self, run_id: str, stats: dict | None = None) -> None:
        self.db.execute("UPDATE runs SET finished_at=?, stats_json=? WHERE id=?",
                        (utcnow(), _json(stats or {}), run_id))

    def record_event(self, item_id: str | None, actor: str, action: str,
                     detail: dict | None = None, run_id: str | None = None) -> None:
        """Append to the audit trail. ``actor`` is ``human`` or ``agent``."""
        self.db.execute(
            "INSERT INTO events (item_id, run_id, ts, actor, action, detail_json) "
            "VALUES (?,?,?,?,?,?)",
            (item_id, run_id, utcnow(), actor, action, _json(detail or {})))

    def list_events(self, item_id: str, limit: int = 100) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM events WHERE item_id=? ORDER BY ts ASC, id ASC LIMIT ?",
            (item_id, int(limit))).fetchall()
        return [{"ts": r["ts"], "actor": r["actor"], "action": r["action"],
                 "detail": _unjson(r["detail_json"])} for r in rows]

    def record_learning(self, *, source_item: str | None, before: str, after: str,
                        lesson: str, applied_to: str = "") -> None:
        """Store one human correction for the weekly coach pass."""
        self.db.execute(
            "INSERT INTO learnings (ts, source_item, before, after, lesson, applied_to) "
            "VALUES (?,?,?,?,?,?)",
            (utcnow(), source_item, before, after, lesson, applied_to))

    def list_learnings(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM learnings ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    # -- kv / cursors / sequences ----------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return _unjson(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, _json(value)))

    def get_cursor(self, name: str, default: Any = None) -> Any:
        """Read a poll cursor, e.g. ``get_cursor("email:last_uid")``."""
        return self.get(f"cursor:{name}", default)

    def set_cursor(self, name: str, value: Any) -> None:
        self.set(f"cursor:{name}", value)

    def next_sequence(self, name: str, *, dry_run: bool = False) -> int:
        """Return the next number in a transactional counter (invoice series).

        On ``dry_run`` the counter is peeked but **not** incremented, so a
        rehearsal can never burn an invoice number.
        """
        row = self.db.execute("SELECT value FROM sequences WHERE name=?", (name,)).fetchone()
        current = row["value"] if row else 0
        if dry_run:
            return current + 1
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT value FROM sequences WHERE name=?", (name,)).fetchone()
            nxt = (row["value"] if row else 0) + 1
            self.db.execute(
                "INSERT INTO sequences (name, value) VALUES (?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, nxt))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return nxt

    # -- reporting --------------------------------------------------------
    def counts(self) -> dict[str, int]:
        """``{review_status: n}`` for every status present. Used by doctor/report."""
        rows = self.db.execute(
            "SELECT review_status, COUNT(*) AS n FROM items GROUP BY review_status").fetchall()
        return {r["review_status"]: r["n"] for r in rows}

    def usage_totals(self, since: str | None = None) -> dict[str, Any]:
        """Aggregate LLM usage recorded by :mod:`core.llm` into ``events``."""
        sql = "SELECT detail_json FROM events WHERE action='llm_call'"
        params: list[Any] = []
        if since:
            sql += " AND ts >= ?"
            params.append(since)
        calls = 0
        totals = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        for row in self.db.execute(sql, params).fetchall():
            detail = _unjson(row["detail_json"]) or {}
            calls += 1
            usage = detail.get("usage") or {}
            totals["input_tokens"] += int(usage.get("input_tokens") or 0)
            totals["output_tokens"] += int(usage.get("output_tokens") or 0)
            totals["cost_usd"] += float(detail.get("cost_usd") or 0.0)
        return {"calls": calls, **totals}
