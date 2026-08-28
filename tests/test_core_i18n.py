"""Tests for core.i18n — language detection and localised template rendering.

Shared across every repo (factory/scaffold/tests/). No network.
"""

from __future__ import annotations

from core.config import HotelConfig, Settings
from core.i18n import (detect_from_text, detect_language, format_date, normalise_phone,
                       strip_accents)


def _settings(languages: list[str]) -> Settings:
    return Settings(hotel=HotelConfig(languages=languages))


def test_booking_language_is_trusted_over_everything_else():
    guess = detect_language("Bonjour, quelle heure est le check-in ?",
                            phone="+49 30 1234567", booking_language="ES",
                            settings=_settings(["en"]))
    assert guess.lang == "es"
    assert guess.source == "booking"


def test_phone_prefix_picks_the_language_when_no_booking_language():
    guess = detect_language("hi", phone="0033 1 23 45 67 89", settings=_settings(["en"]))
    assert guess.lang == "fr"
    assert guess.source == "phone"


def test_country_code_is_used_when_no_phone_or_booking_language():
    guess = detect_language("hi", country="DE", settings=_settings(["en"]))
    assert guess.lang == "de"
    assert guess.source == "country"


def test_text_stopwords_detect_french():
    guess = detect_from_text("Bonjour, merci pour votre reponse au sujet de notre chambre")
    assert guess.lang == "fr"
    assert guess.confidence > 0


def test_falls_back_to_hotel_default_language_with_no_signal():
    guess = detect_language("", settings=_settings(["pt", "en"]))
    assert guess.lang == "pt"
    assert guess.source in ("default", "no-signal")


def test_normalise_phone_strips_noise_and_expands_00_prefix():
    # normalise_phone only strips whitespace/hyphens/parens/dots - it does not
    # drop a national trunk "0", so plain grouped digits round-trip cleanly.
    assert normalise_phone("00 33 6 12-34 56 78") == "+33612345678"
    assert normalise_phone("+1 555 0101") == "+15550101"


def test_format_date_renders_weekday_and_month():
    long_form = format_date("2026-09-14", "en")
    assert "September" in long_form
    assert "14" in long_form
    short_form = format_date("2026-09-14", "en", short=True)
    assert short_form == "14 September"


def test_format_date_localises_to_french():
    assert "septembre" in format_date("2026-09-14", "fr")


def test_strip_accents_for_accent_insensitive_matching():
    assert strip_accents("Château Élysée") == "Chateau Elysee"
