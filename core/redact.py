"""core.redact — strip payment card numbers, CVC/expiry and IBANs from text.

Applied on **ingestion**: anything that arrives from a guest, an OTA relay or a
supplier goes through :func:`redact` before it is written to the store, logged,
or put in an LLM prompt. A card number that never enters the database cannot
leak out of it.

Detection is deliberately conservative, in this order:

1. A 13-19 digit run (spaces/hyphens allowed) that
2. starts with a real payment-network prefix (Visa/Mastercard/Amex/Diners), and
3. passes the Luhn checksum, and
4. is not inside a URL or an email address (long numeric ids pass Luhn by chance).

Only once a card has been found in the same text are labelled ``CVC:``/``Exp:``
values masked too, so a door code ("security code: 1234") or a one-time password
is never eaten.

Redaction must never break ingestion: every function swallows its own errors and
returns the input unchanged if anything goes wrong.
"""

from __future__ import annotations

import re
from typing import Any

CARD_MASK = "[CARD REDACTED ****{last4}]"
IBAN_MASK = "[IBAN REDACTED ****{last4}]"

_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)")
_URLISH = re.compile(r"://|www\.|[?&=]|/\w")
_IBAN = re.compile(r"\b([A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?)\b")

# Label-anchored, and only fired once a PAN was found in the same text.
_CVC_RE = re.compile(
    r"\b(CVC2?|CVV2?|CV2|card code|card security code)(\s*[:#-]?\s*)(\d{3,4})(?!\d)", re.I)
_EXPIRY_RE = re.compile(
    r"\b(exp(?:iry|iration|\.)?(?:\s*date)?)(\s*[:#-]?\s*)(\d{1,2}\s*/\s*\d{2,4})(?!\d)", re.I)


def luhn(digits: str) -> bool:
    """True when ``digits`` satisfies the Luhn (mod 10) checksum."""
    total = 0
    double = False
    for ch in reversed(digits):
        if not ch.isdigit():
            return False
        d = ord(ch) - 48
        if double:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        double = not double
    return total % 10 == 0


def _is_payment_network(digits: str) -> bool:
    if digits[0] in "456":
        return True
    if digits.startswith(("34", "37", "35", "36", "38", "39")):
        return True
    if digits[:3] in ("300", "301", "302", "303", "304", "305"):
        return True
    try:
        return 2221 <= int(digits[:4]) <= 2720
    except ValueError:
        return False


def _in_url_or_email(src: str, offset: int, match_len: int) -> bool:
    """True when the match sits inside a URL-ish or email-ish token."""
    start = offset
    floor = max(0, offset - 2048)
    while start > floor and not src[start - 1].isspace() and src[start - 1] not in "<>\"'":
        start -= 1
    end = offset + match_len
    ceil = min(len(src), end + 2048)
    while end < ceil and not src[end].isspace() and src[end] not in "<>\"'":
        end += 1
    before = src[start:offset]
    after = src[offset + match_len:end]
    return bool(_URLISH.search(before)) or "@" in before or "@" in after \
        or "://" in after or bool(re.search(r"[?&=]", after))


def redact_pan(text: str | None) -> str | None:
    """Replace every payment card number with ``[CARD REDACTED ****last4]``."""
    if not text:
        return text
    try:
        found = [False]

        def _sub(m: "re.Match[str]") -> str:
            digits = re.sub(r"\D", "", m.group(0))
            if not (13 <= len(digits) <= 19):
                return m.group(0)
            if not _is_payment_network(digits):
                return m.group(0)
            if not luhn(digits):
                return m.group(0)
            if _in_url_or_email(m.string, m.start(), len(m.group(0))):
                return m.group(0)
            found[0] = True
            return CARD_MASK.format(last4=digits[-4:])

        out = _CANDIDATE.sub(_sub, text)
        if found[0] or "[CARD REDACTED" in out:
            # Digits abutting a label hide the next label's word boundary until
            # the first mask restores it, so iterate to a fixed point (bounded).
            for _ in range(4):
                prev = out
                out = _EXPIRY_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", out)
                out = _CVC_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", out)
                if out == prev:
                    break
        return out
    except Exception:  # noqa: BLE001 - redaction must never break ingestion
        return text


def redact_iban(text: str | None) -> str | None:
    """Replace bank IBANs with ``[IBAN REDACTED ****last4]``.

    Bank details belong in the accounting system, not in an agent's inbox log.
    """
    if not text:
        return text
    try:
        def _sub(m: "re.Match[str]") -> str:
            raw = m.group(1).replace(" ", "")
            if not (15 <= len(raw) <= 34):
                return m.group(0)
            if not raw[2:4].isdigit() or not raw[:2].isalpha():
                return m.group(0)
            if raw[4:].isdigit() and len(raw) < 18:
                return m.group(0)  # too short / too plain to be an IBAN
            return IBAN_MASK.format(last4=raw[-4:])

        return _IBAN.sub(_sub, text)
    except Exception:  # noqa: BLE001
        return text


def redact(text: str | None, *, cards: bool = True, ibans: bool = True) -> str | None:
    """Full ingestion redaction: cards then IBANs. Safe on ``None``/empty."""
    out = redact_pan(text) if cards else text
    return redact_iban(out) if ibans else out


def redact_obj(obj: Any, *, cards: bool = True, ibans: bool = True) -> Any:
    """Recursively redact every string inside dicts / lists / tuples."""
    if isinstance(obj, str):
        return redact(obj, cards=cards, ibans=ibans)
    if isinstance(obj, dict):
        return {k: redact_obj(v, cards=cards, ibans=ibans) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        seq = [redact_obj(v, cards=cards, ibans=ibans) for v in obj]
        return type(obj)(seq) if isinstance(obj, tuple) else seq
    return obj


def contains_card(text: str | None) -> bool:
    """True when :func:`redact_pan` would mask something in ``text``."""
    return bool(text) and redact_pan(text) != text
