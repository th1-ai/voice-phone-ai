"""Fixture-backed mailbox — no credentials, no network.

Reads ``fixtures/inbound/*.eml`` (real RFC-822 files) and ``fixtures/inbound/*.json``
(simple objects with ``from``, ``subject``, ``body`` and friends). Both forms end
up as :class:`~core.adapters.base.EmailMessage`, so a fixture written by hand and
a real message look identical to the agent.

Sends are guarded like any other adapter and, when allowed, are appended to
``data/exports/sent_email.jsonl`` instead of leaving the machine. That file is
what ``make demo`` shows you at the end.
"""

from __future__ import annotations

import email
import json
from datetime import datetime, timezone
from email import policy
from pathlib import Path
from typing import Any

from core.adapters.base import Email, EmailMessage, HealthCheck, guarded_write
from core.config import repo_root, sub_data_dir
from core.redact import redact


def _body_from_eml(msg: Any) -> tuple[str, str]:
    """Return ``(text, html)`` from a parsed message, preferring text/plain."""
    text, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and not text:
                text = part.get_content()
            elif ctype == "text/html" and not html:
                html = part.get_content()
    else:
        payload = msg.get_content()
        if msg.get_content_type() == "text/html":
            html = payload
        else:
            text = payload
    return text.strip(), html.strip()


class MockEmail(Email):
    """Reads sample messages from ``fixtures/inbound``."""

    status, name = "universal", "email_mock"

    def __init__(self, settings: Any, config: Any = None) -> None:
        super().__init__(settings, config)
        self.dir = Path(self.opt("fixtures_dir") or (repo_root() / "fixtures" / "inbound"))
        self.outbox = sub_data_dir("exports") / "sent_email.jsonl"

    # -- introspection ----------------------------------------------------
    def ping(self) -> HealthCheck:
        if not self.dir.exists():
            return HealthCheck(False, self.name, f"no fixtures at {self.dir}",
                               "Add fixtures/inbound/*.json, or switch "
                               "systems.email.adapter to imap.")
        n = len(self._files())
        return HealthCheck(True, self.name, f"{n} sample messages in {self.dir}")

    def capabilities(self) -> set[str]:
        return {"fetch_unread", "fetch_thread", "send", "mark_read", "label"}

    def _files(self) -> list[Path]:
        return sorted(p for p in self.dir.glob("*")
                      if p.suffix.lower() in (".eml", ".json") and p.is_file())

    # -- reads ------------------------------------------------------------
    def _load(self, path: Path) -> EmailMessage | None:
        if path.suffix.lower() == ".eml":
            parsed = email.message_from_string(path.read_text(encoding="utf-8"),
                                               policy=policy.default)
            text, html = _body_from_eml(parsed)
            from_header = str(parsed.get("From", ""))
            name, addr = email.utils.parseaddr(from_header)
            return EmailMessage(
                id=path.stem, thread_id=str(parsed.get("Thread-Id", path.stem)),
                message_id_header=str(parsed.get("Message-ID", f"<{path.stem}@example.com>")),
                references=str(parsed.get("References", "")),
                from_email=addr, from_name=name,
                to=[a for _, a in email.utils.getaddresses([str(parsed.get("To", ""))]) if a],
                cc=[a for _, a in email.utils.getaddresses([str(parsed.get("Cc", ""))]) if a],
                subject=str(parsed.get("Subject", "")),
                body_text=redact(text) or "", body_html=redact(html) or "",
                received_at=str(parsed.get("Date", "")), folder="INBOX")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return EmailMessage(
            id=str(raw.get("id") or path.stem),
            thread_id=str(raw.get("thread_id") or raw.get("id") or path.stem),
            message_id_header=str(raw.get("message_id") or f"<{path.stem}@example.com>"),
            references=str(raw.get("references") or ""),
            from_email=str(raw.get("from") or raw.get("from_email") or ""),
            from_name=str(raw.get("from_name") or ""),
            to=list(raw.get("to") or []), cc=list(raw.get("cc") or []),
            subject=str(raw.get("subject") or ""),
            body_text=redact(str(raw.get("body") or raw.get("body_text") or "")) or "",
            body_html=redact(str(raw.get("body_html") or "")) or "",
            received_at=str(raw.get("received_at") or ""),
            folder=str(raw.get("folder") or "INBOX"),
            labels=list(raw.get("labels") or []), extra=raw)

    def fetch_unread(self, since: str | None = None, folder: str = "INBOX",
                     limit: int = 50) -> list[EmailMessage]:
        out = []
        for path in self._files():
            msg = self._load(path)
            if msg is None or msg.folder != folder:
                continue
            if since and msg.received_at and msg.received_at < since:
                continue
            out.append(msg)
            if len(out) >= limit:
                break
        return out

    def fetch_thread(self, thread_id: str) -> list[EmailMessage]:
        return [m for m in self.fetch_unread(limit=500) if m.thread_id == thread_id]

    # -- writes -----------------------------------------------------------
    @guarded_write("send_email")
    def send(self, to: str | list[str], subject: str, body_md: str,
             reply_to_message_id: str | None = None, cc: list[str] | None = None,
             attachments: list[str] | None = None) -> dict:
        """Append to ``data/exports/sent_email.jsonl``. Nothing leaves the machine."""
        body_md = self.with_signature(body_md)
        record = {
            "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "to": [to] if isinstance(to, str) else list(to), "cc": list(cc or []),
            "subject": subject, "body": body_md,
            "in_reply_to": reply_to_message_id, "attachments": list(attachments or []),
        }
        with self.outbox.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {"ok": True, "message_id": f"mock-{abs(hash(subject)) % 10**10}",
                "logged_to": str(self.outbox)}

    @guarded_write("email_write")
    def mark_read(self, message_id: str) -> dict:
        return {"ok": True, "message_id": message_id, "note": "mock adapter, nothing changed"}

    @guarded_write("email_write")
    def label(self, message_id: str, label: str) -> dict:
        return {"ok": True, "message_id": message_id, "label": label}
