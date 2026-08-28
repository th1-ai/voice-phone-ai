"""Tests for core.redact — PAN / CVC / expiry / IBAN redaction on ingestion.

Shared across every repo (factory/scaffold/tests/). Pure functions, no I/O.
"""

from __future__ import annotations

from core.redact import contains_card, luhn, redact, redact_iban, redact_obj, redact_pan

VISA = "4111 1111 1111 1111"          # passes Luhn, Visa prefix
NOT_A_CARD = "1234 5678 9012 3456"    # fails Luhn


def test_luhn_checksum_accepts_and_rejects():
    assert luhn("4111111111111111") is True
    assert luhn("1234567890123456") is False


def test_redact_pan_masks_a_real_looking_card_number():
    text = f"My card is {VISA}, please charge it."
    out = redact_pan(text)
    assert VISA not in out
    assert "[CARD REDACTED ****1111]" in out


def test_redact_pan_leaves_non_luhn_digit_runs_alone():
    text = f"Booking reference {NOT_A_CARD} for your records."
    assert redact_pan(text) == text


def test_redact_pan_ignores_digits_inside_a_url():
    url_text = "See https://example.com/track?id=4111111111111111 for status."
    assert redact_pan(url_text) == url_text


def test_cvc_and_expiry_are_masked_once_a_card_is_present():
    text = f"Card {VISA}, exp 09/28, CVC: 123"
    out = redact_pan(text)
    assert "[CARD REDACTED ****1111]" in out
    assert "123" not in out
    assert "09/28" not in out
    assert "[REDACTED]" in out  # expiry masked too, once a card was found


def test_redact_iban_masks_a_real_looking_iban():
    text = "Please wire to FR7630006000011234567890189 today."
    out = redact_iban(text)
    assert "FR7630006000011234567890189" not in out
    assert "[IBAN REDACTED ****0189]" in out


def test_redact_combines_cards_and_ibans():
    text = f"Card {VISA} and IBAN FR7630006000011234567890189."
    out = redact(text)
    assert "[CARD REDACTED" in out
    assert "[IBAN REDACTED" in out


def test_redact_obj_walks_nested_structures():
    payload = {"body": f"pay with {VISA}", "meta": {"note": [f"card {VISA}"]}}
    out = redact_obj(payload)
    assert "[CARD REDACTED" in out["body"]
    assert "[CARD REDACTED" in out["meta"]["note"][0]


def test_contains_card_and_none_safety():
    assert contains_card(f"card number {VISA}") is True
    assert contains_card("no cards here, just text") is False
    assert redact(None) is None
    assert redact("") == ""


def test_logger_follows_repo_root_set_after_construction(tmp_path, monkeypatch):
    """Module-level loggers must write into a sandbox set later (tests) and
    never into the working copy."""
    from core.log import get_logger
    log = get_logger("probe", quiet=True)          # created "at import time"
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    log.info("hello", answer=42)
    files = list((tmp_path / "data" / "logs").glob("*.jsonl"))
    assert files and "hello" in files[0].read_text(encoding="utf-8")
