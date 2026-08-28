"""Universal chat adapter: POST JSON to any URL (Zapier, Make, n8n, your own).

Send-only by design. If your automation platform can receive a webhook, it can
deliver a WhatsApp message, an SMS, a Slack post or a Teams card — the agent does
not need to know which.

``.env``::

    MESSAGING_WEBHOOK_URL=https://hooks.example.com/...
    MESSAGING_WEBHOOK_TOKEN=            # optional; sent as a Bearer token
    MESSAGING_STAFF_WEBHOOK_URL=        # optional; separate URL for staff alerts

The payload is stable, so you can map it once in your automation tool::

    {"chat_id": "...", "text": "...", "kind": "guest" | "staff",
     "hotel": "Hotel Aurora", "sent_at": "2026-09-01T10:00:00+00:00"}

``fetch_new`` is not implemented: a webhook is one-way. Pair this with the email
adapter for inbound, or write an adapter for your provider's inbound API.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from core.adapters._http import HttpError, request_json
from core.adapters.base import (AdapterNotConfigured, AdapterNotImplemented, ChatMessage,
                                HealthCheck, Messaging, guarded_write)


class WebhookMessaging(Messaging):
    """One-way chat delivery through an HTTP webhook."""

    status, name = "universal", "messaging_webhook"

    def __init__(self, settings: Any, config: Any = None) -> None:
        super().__init__(settings, config)
        self.url = str(self.opt("webhook_url", "", env="MESSAGING_WEBHOOK_URL"))
        self.staff_url = str(self.opt("staff_webhook_url", "",
                                      env="MESSAGING_STAFF_WEBHOOK_URL")) or self.url
        self.token = os.environ.get("MESSAGING_WEBHOOK_TOKEN", "")

    def ping(self) -> HealthCheck:
        if not self.url:
            return HealthCheck(False, self.name, "MESSAGING_WEBHOOK_URL is not set",
                               "Add it to .env, or set systems.messaging.adapter: mock.")
        if not self.url.startswith("https://"):
            return HealthCheck(False, self.name, "webhook URL is not https",
                               "Guest messages must not travel over plain http.")
        return HealthCheck(True, self.name, f"will POST to {self.url.split('/')[2]}")

    def capabilities(self) -> set[str]:
        return {"send", "notify_staff"}

    def fetch_new(self, since: str | None = None, limit: int = 50) -> list[ChatMessage]:
        raise AdapterNotImplemented("messaging_webhook", method="fetch_new")

    def _post(self, url: str, chat_id: str, text: str, kind: str) -> dict:
        if not url:
            raise AdapterNotConfigured(
                "messaging_webhook: MESSAGING_WEBHOOK_URL is not set in .env.")
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        payload = {"chat_id": chat_id, "text": text, "kind": kind,
                   "hotel": self.settings.hotel.name,
                   "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        try:
            response = request_json("POST", url, headers=headers, json_body=payload)
        except HttpError as exc:
            raise AdapterNotConfigured(
                f"messaging_webhook: the webhook rejected the message ({exc}). "
                "Check the URL and the token.") from exc
        message_id = ""
        if isinstance(response, dict):
            message_id = str(response.get("id") or response.get("message_id") or "")
        return {"ok": True, "message_id": message_id or "webhook", "response": response}

    @guarded_write("send_message")
    def send(self, chat_id: str, text: str, *, guest_facing: bool = True) -> dict:
        return self._post(self.url, chat_id, self.with_disclosure(text, guest_facing=guest_facing),
                          "guest" if guest_facing else "staff")

    @guarded_write("send_message")
    def notify_staff(self, text: str) -> dict:
        return self._post(self.staff_url, str(self.opt("staff_chat_id", "staff")),
                          text, "staff")
