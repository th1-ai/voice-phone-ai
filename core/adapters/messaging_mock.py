"""Fixture-backed chat channel — no credentials, no network.

Reads ``fixtures/inbound/messages.json`` (a list of message objects) and writes
anything the agent would send to ``data/exports/sent_messages.jsonl``.

Message objects look like::

    {"id": "m1", "chat_id": "c1", "from_number": "+10000000000",
     "from_name": "A guest", "text": "What time is check in?",
     "sent_at": "2026-09-01T10:00:00+00:00"}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.adapters.base import ChatMessage, HealthCheck, Messaging, guarded_write
from core.config import repo_root, sub_data_dir
from core.redact import redact


class MockMessaging(Messaging):
    """Sample chat messages from fixtures; sends go to a local JSONL file."""

    status, name = "universal", "messaging_mock"

    def __init__(self, settings: Any, config: Any = None) -> None:
        super().__init__(settings, config)
        self.path = Path(self.opt("fixtures_file") or
                         (repo_root() / "fixtures" / "inbound" / "messages.json"))
        self.outbox = sub_data_dir("exports") / "sent_messages.jsonl"

    def ping(self) -> HealthCheck:
        if not self.path.exists():
            return HealthCheck(True, self.name, "no message fixtures (that is fine)",
                               f"Add {self.path} to exercise the chat path in demo.")
        return HealthCheck(True, self.name, f"{len(self.fetch_new())} sample messages")

    def capabilities(self) -> set[str]:
        return {"fetch_new", "send", "notify_staff"}

    def fetch_new(self, since: str | None = None, limit: int = 50) -> list[ChatMessage]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        rows = raw if isinstance(raw, list) else raw.get("messages") or []
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            sent_at = str(row.get("sent_at") or "")
            if since and sent_at and sent_at < since:
                continue
            out.append(ChatMessage(
                id=str(row.get("id") or ""), chat_id=str(row.get("chat_id") or ""),
                from_number=str(row.get("from_number") or ""),
                from_name=str(row.get("from_name") or ""),
                text=redact(str(row.get("text") or "")) or "",
                sent_at=sent_at, direction=str(row.get("direction") or "in"), extra=row))
            if len(out) >= limit:
                break
        return out

    def _log(self, chat_id: str, text: str, kind: str) -> dict:
        record = {"sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "chat_id": chat_id, "text": text, "kind": kind}
        with self.outbox.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {"ok": True, "message_id": f"mock-{abs(hash(text)) % 10**10}",
                "logged_to": str(self.outbox)}

    @guarded_write("send_message")
    def send(self, chat_id: str, text: str, *, guest_facing: bool = True) -> dict:
        return self._log(chat_id, self.with_disclosure(text, guest_facing=guest_facing),
                         "guest" if guest_facing else "staff")

    @guarded_write("send_message")
    def notify_staff(self, text: str) -> dict:
        return self._log(str(self.opt("staff_chat_id", "staff")), text, "staff")
