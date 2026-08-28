"""Gmail adapter — status: built. Uses the Gmail API with a desktop OAuth flow.

Choose this over ``email_imap`` when you want Gmail labels, threads as Gmail
understands them, or you are on Google Workspace with IMAP disabled by policy.

**Setup (about ten minutes, once).**

1. In Google Cloud Console create a project and enable the **Gmail API**.
2. Configure the OAuth consent screen (Internal for Workspace, External + your
   own address as a test user otherwise).
3. Create an OAuth client of type **Desktop app** and download the JSON.
4. Save it as ``credentials.json`` in this repo (it is gitignored).
5. Install the client libraries::

       pip install google-api-python-client google-auth-oauthlib

6. Run ``make doctor``. The first run opens a browser once and writes
   ``token.json`` next to the credentials. After that it refreshes silently.

**Scopes requested** (least privilege that still lets the agent reply):

``https://www.googleapis.com/auth/gmail.readonly``  read messages and threads
``https://www.googleapis.com/auth/gmail.send``      send, including replies
``https://www.googleapis.com/auth/gmail.modify``    mark read, add labels

Drop ``gmail.modify`` from :data:`SCOPES` if you do not want the agent touching
labels; ``mark_read`` and ``label`` then report as unavailable, which is honest
and harmless.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, parseaddr
from pathlib import Path
from typing import Any

from core.adapters.base import (AdapterNotConfigured, Email, EmailMessage, HealthCheck,
                                guarded_write)
from core.config import repo_root
from core.redact import redact

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

_INSTALL_HINT = ("pip install google-api-python-client google-auth-oauthlib "
                 "(or switch systems.email.adapter to imap, which needs no libraries)")


class GmailEmail(Email):
    """Gmail API adapter. Needs ``credentials.json`` and a one-time browser login."""

    status, name = "built", "email_gmail"

    def __init__(self, settings: Any, config: Any = None) -> None:
        super().__init__(settings, config)
        self.address = str(self.opt("mailbox", "", env="EMAIL_ADDRESS"))
        self.credentials_file = Path(self.opt("credentials_file",
                                              repo_root() / "credentials.json",
                                              env="GOOGLE_CREDENTIALS_FILE"))
        self.token_file = Path(self.opt("token_file", repo_root() / "token.json",
                                        env="GOOGLE_TOKEN_FILE"))
        self.from_name = str(self.opt("from_name", settings.hotel.name,
                                      env="EMAIL_FROM_NAME"))
        self._service: Any = None

    # -- auth -------------------------------------------------------------
    def _client(self) -> Any:
        if self._service is not None:
            return self._service
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise AdapterNotConfigured(
                f"email_gmail: Google client libraries are not installed. {_INSTALL_HINT}"
            ) from exc

        creds = None
        if self.token_file.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds or not creds.valid:
            if not self.credentials_file.exists():
                raise AdapterNotConfigured(
                    f"email_gmail: {self.credentials_file} not found. Download the OAuth "
                    "desktop client JSON from Google Cloud Console and save it there "
                    "(see the module docstring or docs/integrations.md#email).")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.credentials_file), SCOPES)
            creds = flow.run_local_server(port=0)
        self.token_file.write_text(creds.to_json(), encoding="utf-8")
        try:
            self.token_file.chmod(0o600)
        except OSError:
            pass
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    # -- introspection ----------------------------------------------------
    def ping(self) -> HealthCheck:
        if not self.credentials_file.exists() and not self.token_file.exists():
            return HealthCheck(
                False, self.name, f"no {self.credentials_file.name} and no token",
                "Follow docs/integrations.md#email to create a Gmail OAuth desktop client.")
        try:
            profile = self._client().users().getProfile(userId="me").execute()
        except Exception as exc:  # noqa: BLE001 - ping never raises
            return HealthCheck(False, self.name, str(exc)[:200],
                               "Delete token.json and run `make doctor` to re-authorise.")
        return HealthCheck(True, self.name,
                           f"{profile.get('emailAddress')} "
                           f"({profile.get('messagesTotal', 0)} messages)")

    def capabilities(self) -> set[str]:
        caps = {"fetch_unread", "fetch_thread", "send"}
        if "https://www.googleapis.com/auth/gmail.modify" in SCOPES:
            caps |= {"mark_read", "label"}
        return caps

    # -- reads ------------------------------------------------------------
    @staticmethod
    def _header(payload: dict, name: str) -> str:
        for header in payload.get("headers") or []:
            if str(header.get("name", "")).lower() == name.lower():
                return str(header.get("value", ""))
        return ""

    @staticmethod
    def _decode_parts(payload: dict) -> tuple[str, str]:
        text, html = "", ""
        stack = [payload]
        while stack:
            part = stack.pop()
            mime = part.get("mimeType", "")
            body = (part.get("body") or {}).get("data")
            if body:
                decoded = base64.urlsafe_b64decode(body).decode("utf-8", "replace")
                if mime == "text/plain" and not text:
                    text = decoded
                elif mime == "text/html" and not html:
                    html = decoded
            stack.extend(part.get("parts") or [])
        return text.strip(), html.strip()

    def _to_message(self, raw: dict) -> EmailMessage:
        payload = raw.get("payload") or {}
        text, html = self._decode_parts(payload)
        name, addr = parseaddr(self._header(payload, "From"))
        received = raw.get("internalDate")
        received_at = (datetime.fromtimestamp(int(received) / 1000, timezone.utc)
                       .isoformat(timespec="seconds") if received else "")
        return EmailMessage(
            id=str(raw.get("id", "")), thread_id=str(raw.get("threadId", "")),
            message_id_header=self._header(payload, "Message-ID"),
            references=self._header(payload, "References"),
            from_email=addr, from_name=name,
            to=[a.strip() for a in self._header(payload, "To").split(",") if a.strip()],
            cc=[a.strip() for a in self._header(payload, "Cc").split(",") if a.strip()],
            subject=self._header(payload, "Subject"),
            body_text=redact(text) or "", body_html=redact(html) or "",
            received_at=received_at, labels=list(raw.get("labelIds") or []))

    def fetch_unread(self, since: str | None = None, folder: str = "INBOX",
                     limit: int = 50) -> list[EmailMessage]:
        """Unread messages. ``since`` is ``YYYY-MM-DD``; Gmail's ``after:`` is day-granular."""
        query = "is:unread"
        if folder and folder.upper() != "ALL":
            query += f" in:{folder.lower()}"
        if since:
            query += f" after:{since[:10].replace('-', '/')}"
        service = self._client()
        listing = service.users().messages().list(
            userId="me", q=query, maxResults=int(limit)).execute()
        out = []
        for stub in listing.get("messages") or []:
            raw = service.users().messages().get(
                userId="me", id=stub["id"], format="full").execute()
            out.append(self._to_message(raw))
        return out

    def fetch_thread(self, thread_id: str) -> list[EmailMessage]:
        thread = self._client().users().threads().get(
            userId="me", id=thread_id, format="full").execute()
        return [self._to_message(m) for m in thread.get("messages") or []]

    # -- writes -----------------------------------------------------------
    @guarded_write("send_email")
    def send(self, to: str | list[str], subject: str, body_md: str,
             reply_to_message_id: str | None = None, cc: list[str] | None = None,
             attachments: list[str] | None = None) -> dict:
        """Send, threading into an existing conversation when replying.

        ``reply_to_message_id`` is the Gmail message id being answered. Its
        ``Message-ID`` header and thread id are looked up so the reply lands in
        the same conversation.
        """
        body_md = self.with_signature(body_md)
        service = self._client()
        thread_id, in_reply_to, references = None, "", ""
        if reply_to_message_id:
            original = service.users().messages().get(
                userId="me", id=reply_to_message_id, format="metadata",
                metadataHeaders=["Message-ID", "References", "Subject"]).execute()
            payload = original.get("payload") or {}
            thread_id = original.get("threadId")
            in_reply_to = self._header(payload, "Message-ID")
            references = (self._header(payload, "References") + " " + in_reply_to).strip()

        msg = MimeMessage()
        msg["From"] = formataddr((self.from_name, self.address or "me"))
        msg["To"] = to if isinstance(to, str) else ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = references
        msg.set_content(body_md)
        for path_str in attachments or []:
            path = Path(path_str)
            if path.exists():
                msg.add_attachment(path.read_bytes(), maintype="application",
                                   subtype="octet-stream", filename=path.name)

        body: dict[str, Any] = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if thread_id:
            body["threadId"] = thread_id
        sent = service.users().messages().send(userId="me", body=body).execute()
        return {"ok": True, "message_id": sent.get("id"), "thread_id": sent.get("threadId")}

    @guarded_write("email_write")
    def mark_read(self, message_id: str) -> dict:
        self._client().users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}).execute()
        return {"ok": True, "message_id": message_id}

    @guarded_write("email_write")
    def label(self, message_id: str, label: str) -> dict:
        """Add a label, creating it if the mailbox does not have it yet."""
        service = self._client()
        existing = service.users().labels().list(userId="me").execute().get("labels") or []
        match = next((l for l in existing if l.get("name") == label), None)
        if match is None:
            match = service.users().labels().create(
                userId="me", body={"name": label, "labelListVisibility": "labelShow",
                                   "messageListVisibility": "show"}).execute()
        service.users().messages().modify(
            userId="me", id=message_id, body={"addLabelIds": [match["id"]]}).execute()
        return {"ok": True, "message_id": message_id, "label": label}
