"""WhatsApp (and other chat networks) through the hotel's OWN UniPile account.

UniPile is a hosted bridge to WhatsApp, Instagram, LinkedIn and others. The
hotel creates their own UniPile account, connects their own WhatsApp number by
scanning a QR code, and puts their own credentials in ``.env``. Nothing here
routes through anybody else's account.

``.env``::

    UNIPILE_DSN=api1.unipile.com:13111     # your account's host:port, from the dashboard
    UNIPILE_API_KEY=...                    # your account's API key
    UNIPILE_ACCOUNT_ID=...                 # the connected WhatsApp account id
    UNIPILE_STAFF_CHAT_ID=                 # optional: where staff alerts go

**Before you turn this on.** WhatsApp Business policy restricts what you may send
and when. Guest-initiated conversations have a service window; outside it you
generally need approved template messages. Read your provider's rules, and see
``docs/safety.md`` for the disclosure line every automated guest message should
carry.

Sends are guarded like every other write, so in shadow mode the agent drafts the
message and queues it for review instead of sending it.
"""

from __future__ import annotations

import os
from typing import Any

from core.adapters._http import HttpError, request_json
from core.adapters.base import (AdapterNotConfigured, ChatMessage, HealthCheck, Messaging,
                                guarded_write)
from core.redact import redact


class UnipileMessaging(Messaging):
    """Chat over the hotel's own UniPile account."""

    status, name = "built", "messaging_unipile"

    def __init__(self, settings: Any, config: Any = None) -> None:
        super().__init__(settings, config)
        dsn = str(self.opt("dsn", "", env="UNIPILE_DSN")).strip()
        dsn = dsn.replace("https://", "").replace("http://", "").rstrip("/")
        self.base = f"https://{dsn}/api/v1" if dsn else ""
        self.account_id = str(self.opt("account_id", "", env="UNIPILE_ACCOUNT_ID"))
        self.staff_chat_id = str(self.opt("staff_chat_id", "",
                                          env="UNIPILE_STAFF_CHAT_ID"))

    # -- plumbing ---------------------------------------------------------
    def _headers(self) -> dict:
        key = os.environ.get("UNIPILE_API_KEY", "")
        if not (self.base and key and self.account_id):
            raise AdapterNotConfigured(
                "messaging_unipile: set UNIPILE_DSN, UNIPILE_API_KEY and "
                "UNIPILE_ACCOUNT_ID in .env. They come from your own UniPile dashboard "
                "after you connect your WhatsApp number.")
        return {"X-API-KEY": key, "Accept": "application/json"}

    def _get(self, path: str, params: dict | None = None) -> Any:
        return request_json("GET", f"{self.base}/{path}", headers=self._headers(),
                            params=params)

    # -- introspection ----------------------------------------------------
    def ping(self) -> HealthCheck:
        missing = [v for v in ("UNIPILE_DSN", "UNIPILE_API_KEY", "UNIPILE_ACCOUNT_ID")
                   if not os.environ.get(v)]
        if missing:
            return HealthCheck(False, self.name, f"missing {', '.join(missing)}",
                               "Fill them from your UniPile dashboard — see "
                               "docs/integrations.md#messaging.")
        try:
            data = self._get(f"accounts/{self.account_id}")
        except (HttpError, AdapterNotConfigured) as exc:
            return HealthCheck(False, self.name, str(exc)[:200],
                               "Check the API key and that the account is still connected "
                               "(WhatsApp sessions expire and need a re-scan).")
        status = str((data or {}).get("status") or (data or {}).get("sources", [{}])[0]
                     .get("status", "unknown"))
        ok = status.upper() in ("OK", "CONNECTED", "SYNCED")
        return HealthCheck(ok, self.name, f"account {self.account_id} status={status}",
                           "" if ok else "Reconnect the account in the UniPile dashboard.")

    def capabilities(self) -> set[str]:
        return {"fetch_new", "send", "notify_staff", "list_chats"}

    # -- reads ------------------------------------------------------------
    def list_chats(self, limit: int = 50) -> list[dict]:
        """Conversations on the connected account, newest first."""
        data = self._get("chats", {"account_id": self.account_id, "limit": limit})
        return [c for c in (data or {}).get("items", []) if isinstance(c, dict)]

    def fetch_new(self, since: str | None = None, limit: int = 50) -> list[ChatMessage]:
        """Messages received since ``since`` (ISO timestamp) across all chats."""
        params: dict[str, Any] = {"account_id": self.account_id, "limit": limit}
        if since:
            params["after"] = since
        data = self._get("messages", params)
        out = []
        for raw in (data or {}).get("items", []):
            if not isinstance(raw, dict):
                continue
            if str(raw.get("is_sender") or "") in ("1", "True", "true"):
                continue  # our own outbound message
            out.append(ChatMessage(
                id=str(raw.get("id") or ""), chat_id=str(raw.get("chat_id") or ""),
                from_number=str(raw.get("sender_attendee_id") or raw.get("from") or ""),
                from_name=str(raw.get("sender_name") or ""),
                text=redact(str(raw.get("text") or "")) or "",
                sent_at=str(raw.get("timestamp") or ""), direction="in", extra=raw))
        return out

    # -- writes -----------------------------------------------------------
    def _send_raw(self, chat_id: str, text: str) -> dict:
        """The actual POST. Private, so the guard is never accidentally skipped."""
        if not chat_id:
            raise AdapterNotConfigured(
                "messaging_unipile: send() needs a chat_id. Use list_chats() to find it; "
                "WhatsApp will not let you open a conversation with a stranger.")
        boundary = "----unipile-boundary"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n"
                f"{text}\r\n--{boundary}--\r\n").encode("utf-8")
        headers = {**self._headers(),
                   "Content-Type": f"multipart/form-data; boundary={boundary}"}
        response = request_json("POST", f"{self.base}/chats/{chat_id}/messages",
                                headers=headers, data=body)
        return {"ok": True, "message_id": str((response or {}).get("message_id") or ""),
                "chat_id": chat_id}

    @guarded_write("send_message")
    def send(self, chat_id: str, text: str, *, guest_facing: bool = True) -> dict:
        """Send into an existing chat. UniPile needs the chat id, not a phone number."""
        return self._send_raw(chat_id, self.with_disclosure(text, guest_facing=guest_facing))

    @guarded_write("send_message")
    def notify_staff(self, text: str) -> dict:
        """Send an internal alert to the staff chat (often the hotel's own self-chat)."""
        if not self.staff_chat_id:
            raise AdapterNotConfigured(
                "messaging_unipile: UNIPILE_STAFF_CHAT_ID is not set, so there is nowhere "
                "to send staff alerts. Set it, or use systems.messaging.staff_chat_id.")
        return self._send_raw(self.staff_chat_id, text)
