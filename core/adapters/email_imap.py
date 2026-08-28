"""Universal mailbox adapter: IMAP to read, SMTP to send.

Works with any mail provider that speaks IMAP and SMTP, which is all of them.
No API keys, no OAuth app to register, no vendor lock-in. This is the adapter to
start with unless you specifically need Gmail labels.

``.env``::

    EMAIL_ADDRESS=reservations@example.com
    EMAIL_PASSWORD=                 # an APP password, never the account password
    IMAP_HOST=imap.example.com
    IMAP_PORT=993
    SMTP_HOST=smtp.example.com
    SMTP_PORT=587                   # 587 = STARTTLS, 465 = implicit TLS
    EMAIL_FROM_NAME=Hotel Aurora

**Use an app-specific password.** Google, Microsoft and Fastmail all issue them.
Two-factor stays on and the password can be revoked without touching the account.

**Threading is done properly.** A reply carries ``In-Reply-To`` and ``References``
built from the message it answers, so it lands inside the guest's existing thread
rather than starting a new one. Getting this wrong is the single most common way
an email agent looks broken to a guest.

Every message body is redacted on ingestion (:mod:`core.redact`) before it is
stored or shown to a model.
"""

from __future__ import annotations

import email
import imaplib
import smtplib
import ssl
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr
from pathlib import Path
from typing import Any

from core.adapters.base import (AdapterNotConfigured, Email, EmailMessage, HealthCheck,
                                guarded_write)
from core.redact import redact


def _md_to_html(text: str) -> str:
    """Minimal markdown-ish to HTML: paragraphs and line breaks only."""
    paragraphs = [p.strip() for p in (text or "").strip().split("\n\n") if p.strip()]
    return "\n".join("<p>" + p.replace("\n", "<br>\n") + "</p>" for p in paragraphs)


class ImapEmail(Email):
    """IMAP read + SMTP send. Works with any provider."""

    status, name = "universal", "email_imap"

    def __init__(self, settings: Any, config: Any = None) -> None:
        super().__init__(settings, config)
        self.address = str(self.opt("mailbox", "", env="EMAIL_ADDRESS"))
        self.imap_host = str(self.opt("imap_host", "", env="IMAP_HOST"))
        self.imap_port = int(self.opt("imap_port", 993, env="IMAP_PORT") or 993)
        self.smtp_host = str(self.opt("smtp_host", "", env="SMTP_HOST"))
        self.smtp_port = int(self.opt("smtp_port", 587, env="SMTP_PORT") or 587)
        self.from_name = str(self.opt("from_name", settings.hotel.name,
                                      env="EMAIL_FROM_NAME"))
        self.signature_file = self.opt("signature_file", "")

    # -- connections ------------------------------------------------------
    def _password(self) -> str:
        import os
        password = os.environ.get("EMAIL_PASSWORD", "")
        if not password:
            raise AdapterNotConfigured(
                "email_imap: EMAIL_PASSWORD is not set. Create an app-specific password "
                "with your mail provider and put it in .env (never your login password).")
        return password

    def _imap(self) -> imaplib.IMAP4_SSL:
        if not (self.imap_host and self.address):
            raise AdapterNotConfigured(
                "email_imap: set IMAP_HOST and EMAIL_ADDRESS in .env.")
        conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port,
                                 ssl_context=ssl.create_default_context())
        conn.login(self.address, self._password())
        return conn

    def _smtp(self) -> smtplib.SMTP:
        if not self.smtp_host:
            raise AdapterNotConfigured("email_imap: set SMTP_HOST in .env.")
        context = ssl.create_default_context()
        if self.smtp_port == 465:
            server: smtplib.SMTP = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port,
                                                    context=context, timeout=30)
        else:
            server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30)
            server.starttls(context=context)
        server.login(self.address, self._password())
        return server

    # -- introspection ----------------------------------------------------
    def ping(self) -> HealthCheck:
        import os
        missing = [v for v in ("EMAIL_ADDRESS", "EMAIL_PASSWORD", "IMAP_HOST")
                   if not os.environ.get(v) and not self.opt(v.lower())]
        if missing:
            return HealthCheck(False, self.name, f"missing {', '.join(missing)}",
                               "Fill them in .env (docs/integrations.md#email), or set "
                               "systems.email.adapter: mock while you set it up.")
        try:
            conn = self._imap()
            status, data = conn.select("INBOX", readonly=True)
            conn.logout()
        except (imaplib.IMAP4.error, OSError, AdapterNotConfigured) as exc:
            return HealthCheck(False, self.name, f"IMAP login failed: {exc}"[:200],
                               "Check the host, the address and that you used an "
                               "app-specific password.")
        count = int(data[0]) if status == "OK" and data and data[0] else 0
        return HealthCheck(True, self.name, f"INBOX reachable, {count} messages")

    def capabilities(self) -> set[str]:
        return {"fetch_unread", "fetch_thread", "send", "mark_read"}

    # -- reads ------------------------------------------------------------
    def _parse(self, uid: str, raw: bytes, folder: str) -> EmailMessage:
        parsed = email.message_from_bytes(raw, policy=policy.default)
        text, html = "", ""
        if parsed.is_multipart():
            for part in parsed.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain" and not text:
                    text = part.get_content()
                elif ctype == "text/html" and not html:
                    html = part.get_content()
        else:
            body = parsed.get_content()
            if parsed.get_content_type() == "text/html":
                html = body
            else:
                text = body
        name, addr = parseaddr(str(parsed.get("From", "")))
        return EmailMessage(
            id=uid, thread_id=str(parsed.get("References", "") or
                                  parsed.get("Message-ID", uid)).split()[0],
            message_id_header=str(parsed.get("Message-ID", "")),
            references=str(parsed.get("References", "")),
            from_email=addr, from_name=name,
            to=[a for _, a in getaddresses([str(parsed.get("To", ""))]) if a],
            cc=[a for _, a in getaddresses([str(parsed.get("Cc", ""))]) if a],
            subject=str(parsed.get("Subject", "")),
            body_text=redact(text.strip()) or "", body_html=redact(html.strip()) or "",
            received_at=str(parsed.get("Date", "")), folder=folder)

    def fetch_unread(self, since: str | None = None, folder: str = "INBOX",
                     limit: int = 50) -> list[EmailMessage]:
        """Unseen messages, optionally only those after ``since`` (``YYYY-MM-DD``).

        IMAP ``SINCE`` is day-granular, so always pair this with
        ``store.already_processed()`` to skip what you handled yesterday.
        """
        conn = self._imap()
        try:
            conn.select(folder, readonly=True)
            criteria = ["UNSEEN"]
            if since:
                try:
                    day = datetime.fromisoformat(since[:19].replace("Z", ""))
                    criteria += ["SINCE", day.strftime("%d-%b-%Y")]
                except ValueError:
                    pass
            status, data = conn.search(None, *criteria)
            if status != "OK" or not data or not data[0]:
                return []
            uids = data[0].split()[-limit:]
            out = []
            for uid in uids:
                status, payload = conn.fetch(uid, "(RFC822)")
                if status != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                out.append(self._parse(uid.decode(), payload[0][1], folder))
            return out
        finally:
            try:
                conn.logout()
            except OSError:
                pass

    def fetch_thread(self, thread_id: str) -> list[EmailMessage]:
        """Everything whose References or Message-ID mentions ``thread_id``."""
        conn = self._imap()
        try:
            conn.select("INBOX", readonly=True)
            status, data = conn.search(None, "HEADER", "References", thread_id)
            uids = data[0].split() if status == "OK" and data and data[0] else []
            out = []
            for uid in uids[-50:]:
                status, payload = conn.fetch(uid, "(RFC822)")
                if status == "OK" and payload and isinstance(payload[0], tuple):
                    out.append(self._parse(uid.decode(), payload[0][1], "INBOX"))
            return out
        finally:
            try:
                conn.logout()
            except OSError:
                pass

    # -- writes -----------------------------------------------------------
    @guarded_write("send_email")
    def send(self, to: str | list[str], subject: str, body_md: str,
             reply_to_message_id: str | None = None, cc: list[str] | None = None,
             attachments: list[str] | None = None) -> dict:
        """Send a multipart text+HTML message, threaded when replying.

        ``reply_to_message_id`` is the ``Message-ID`` header of the message being
        answered (not the IMAP uid). Pass it and the reply threads correctly.
        """
        recipients = [to] if isinstance(to, str) else list(to)
        text_body = self.with_signature(body_md)

        msg = MimeMessage()
        msg["From"] = formataddr((self.from_name, self.address))
        msg["To"] = ", ".join(recipients)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self.address.split("@")[-1] or "example.com")
        if reply_to_message_id:
            msg["In-Reply-To"] = reply_to_message_id
            msg["References"] = reply_to_message_id
        msg.set_content(text_body)
        msg.add_alternative(_md_to_html(text_body), subtype="html")

        for path_str in attachments or []:
            path = Path(path_str)
            if not path.exists():
                continue
            msg.add_attachment(path.read_bytes(), maintype="application",
                               subtype="octet-stream", filename=path.name)

        server = self._smtp()
        try:
            server.send_message(msg, to_addrs=recipients + list(cc or []))
        finally:
            server.quit()
        return {"ok": True, "message_id": msg["Message-ID"],
                "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    @guarded_write("email_write")
    def mark_read(self, message_id: str) -> dict:
        conn = self._imap()
        try:
            conn.select("INBOX")
            conn.store(message_id.encode(), "+FLAGS", "\\Seen")
        finally:
            try:
                conn.logout()
            except OSError:
                pass
        return {"ok": True, "message_id": message_id}
