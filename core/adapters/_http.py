"""Tiny stdlib HTTP helper shared by the network adapters.

The templates deliberately avoid a ``requests`` dependency: ``urllib`` is in the
standard library, so a hotel can run the agent on a stock Python with only
``pyyaml`` installed. This module wraps the awkward parts (JSON encoding, form
encoding, error bodies, retry with backoff, a simple rate limiter).

Not an adapter itself — nothing here knows about hotels.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from typing import Any


class HttpError(RuntimeError):
    """An HTTP response we could not use. ``status`` and ``body`` are preserved."""

    def __init__(self, status: int, url: str, body: str = "") -> None:
        super().__init__(f"HTTP {status} for {url}: {body[:400]}")
        self.status, self.url, self.body = status, url, body


class RateLimiter:
    """Thread-safe sliding window: at most ``max_calls`` per ``period`` seconds."""

    def __init__(self, max_calls: int = 4, period: float = 1.0) -> None:
        self.max_calls, self.period = max_calls, period
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._times and now - self._times[0] >= self.period:
                self._times.popleft()
            if len(self._times) >= self.max_calls:
                sleep_for = self._times[0] + self.period - now
                if sleep_for > 0:
                    time.sleep(sleep_for)
            self._times.append(time.monotonic())


def request(method: str, url: str, *, headers: dict | None = None,
            params: dict | None = None, json_body: Any = None, form: dict | None = None,
            data: bytes | None = None, timeout: int = 30,
            retries: int = 2, backoff: float = 1.0) -> tuple[int, bytes, dict]:
    """Perform one HTTP request. Retries 429 and 5xx with exponential backoff.

    Returns ``(status, body_bytes, headers)``. Raises :class:`HttpError` only
    after the retries are used up.
    """
    if params:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None},
                                       doseq=True)
        url = f"{url}{'&' if '?' in url else '?'}{query}"
    send_headers = dict(headers or {})
    body = data
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False, default=str).encode("utf-8")
        send_headers.setdefault("Content-Type", "application/json")
    elif form is not None:
        body = urllib.parse.urlencode(form, doseq=True).encode("utf-8")
        send_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=send_headers,
                                     method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                last = exc
                time.sleep(backoff * (2 ** attempt))
                continue
            return exc.code, payload, dict(exc.headers or {})
        except urllib.error.URLError as exc:
            if attempt < retries:
                last = exc
                time.sleep(backoff * (2 ** attempt))
                continue
            raise HttpError(0, url, str(exc.reason)) from exc
    raise HttpError(0, url, str(last))  # pragma: no cover


def request_json(method: str, url: str, **kwargs: Any) -> Any:
    """:func:`request` plus JSON decoding, raising :class:`HttpError` on 4xx/5xx."""
    status, body, _ = request(method, url, **kwargs)
    text = body.decode("utf-8", "replace")
    if status >= 400:
        raise HttpError(status, url, text)
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise HttpError(status, url, f"response was not JSON: {text[:200]}") from exc
