"""core.i18n — guest language detection and localised template rendering.

Hotels get mail in whatever language the guest writes in. The agent has to pick
a reply language before it drafts, and it has to do it without spending a model
call on every message.

:func:`detect_language` uses three signals, best first:

1. an explicit language on the booking (a PMS preference field) — trusted;
2. the phone country code, then the country field — cheap and reliable;
3. a stopword vote over the message text — free, and good enough to separate the
   eight languages the templates ship in.

Supported codes: ``en fr de es it pt nl sv``. Anything else falls back to the
first entry of ``hotel.languages``. Add a language by adding its stopwords to
``STOPWORDS`` and a ``<name>.<lang>.md`` template.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import Settings, repo_root
from core.templates import render_string, split_frontmatter

LANGUAGES = ("en", "fr", "de", "es", "it", "pt", "nl", "sv")

#: high-frequency function words. Short lists beat long ones: they are the words
#: that actually differ between these eight languages.
STOPWORDS: dict[str, set[str]] = {
    "en": {"the", "and", "you", "for", "with", "please", "would", "could", "we", "is",
           "our", "your", "have", "room", "booking", "thanks", "thank", "regards"},
    "fr": {"le", "la", "les", "des", "une", "vous", "nous", "pour", "avec", "bonjour",
           "merci", "chambre", "reservation", "réservation", "cordialement", "est", "je"},
    "de": {"der", "die", "das", "und", "sie", "wir", "für", "mit", "bitte", "danke",
           "zimmer", "buchung", "guten", "freundlichen", "grüßen", "ich", "ist"},
    "es": {"el", "la", "los", "las", "una", "por", "para", "con", "gracias", "hola",
           "habitación", "habitacion", "reserva", "saludos", "usted", "somos", "es"},
    "it": {"il", "lo", "gli", "una", "per", "con", "grazie", "buongiorno", "camera",
           "prenotazione", "cordiali", "saluti", "siamo", "sono", "vorrei"},
    "pt": {"o", "os", "as", "uma", "para", "com", "obrigado", "obrigada", "olá", "ola",
           "quarto", "reserva", "cumprimentos", "somos", "gostaria", "não", "nao"},
    "nl": {"de", "het", "een", "en", "voor", "met", "bedankt", "graag", "kamer",
           "boeking", "vriendelijke", "groeten", "wij", "zijn", "ik", "alvast"},
    "sv": {"och", "att", "för", "med", "tack", "hej", "rum", "bokning", "vänliga",
           "hälsningar", "vi", "är", "jag", "gärna", "till"},
}

#: characters that only really appear in one or two of the eight
_HINT_CHARS = {"ß": "de", "ñ": "es", "å": "sv", "ø": "nl", "ã": "pt", "õ": "pt", "ç": "pt"}

#: phone country codes -> language. Longest prefix wins.
PHONE_PREFIXES: dict[str, str] = {
    "+33": "fr", "+32": "nl", "+352": "fr", "+41": "de", "+49": "de", "+43": "de",
    "+34": "es", "+39": "it", "+351": "pt", "+31": "nl", "+46": "sv", "+44": "en",
    "+353": "en", "+1": "en", "+61": "en", "+64": "en", "+55": "pt", "+52": "es",
}

COUNTRY_CODES: dict[str, str] = {
    "GB": "en", "US": "en", "IE": "en", "AU": "en", "NZ": "en", "CA": "en",
    "FR": "fr", "MC": "fr", "LU": "fr", "DE": "de", "AT": "de", "CH": "de",
    "ES": "es", "MX": "es", "AR": "es", "IT": "it", "PT": "pt", "BR": "pt",
    "NL": "nl", "BE": "nl", "SE": "sv",
}

DAYS = {
    "en": "Monday Tuesday Wednesday Thursday Friday Saturday Sunday",
    "fr": "lundi mardi mercredi jeudi vendredi samedi dimanche",
    "de": "Montag Dienstag Mittwoch Donnerstag Freitag Samstag Sonntag",
    "es": "lunes martes miércoles jueves viernes sábado domingo",
    "it": "lunedì martedì mercoledì giovedì venerdì sabato domenica",
    "pt": "segunda terça quarta quinta sexta sábado domingo",
    "nl": "maandag dinsdag woensdag donderdag vrijdag zaterdag zondag",
    "sv": "måndag tisdag onsdag torsdag fredag lördag söndag",
}
MONTHS = {
    "en": "January February March April May June July August September October November December",
    "fr": "janvier février mars avril mai juin juillet août septembre octobre novembre décembre",
    "de": "Januar Februar März April Mai Juni Juli August September Oktober November Dezember",
    "es": "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre",
    "it": "gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre",
    "pt": "janeiro fevereiro março abril maio junho julho agosto setembro outubro novembro dezembro",
    "nl": "januari februari maart april mei juni juli augustus september oktober november december",
    "sv": "januari februari mars april maj juni juli augusti september oktober november december",
}


@dataclass
class LanguageGuess:
    """``lang`` is a two-letter code; ``source`` says which signal decided it."""

    lang: str
    source: str
    confidence: float = 0.0

    def __str__(self) -> str:  # so f"{guess}" reads as just the code
        return self.lang


def normalise_phone(phone: str) -> str:
    """``00 33 …`` and ``(0)`` noise -> ``+33…``."""
    n = re.sub(r"[\s\-().]", "", phone or "")
    if n.startswith("00"):
        n = "+" + n[2:]
    return n


def _tokens(text: str) -> list[str]:
    lowered = (text or "").lower()
    return re.findall(r"[a-zà-öø-ÿ']{2,}", lowered)


def detect_from_text(text: str) -> LanguageGuess:
    """Stopword vote over the message body. Ties break towards English."""
    tokens = _tokens(text)
    if not tokens:
        return LanguageGuess("en", "empty", 0.0)
    scores = {lang: 0 for lang in LANGUAGES}
    for token in tokens:
        for lang, words in STOPWORDS.items():
            if token in words:
                scores[lang] += 1
    lowered = (text or "").lower()
    for char, lang in _HINT_CHARS.items():
        if char in lowered:
            scores[lang] += 2
    best = max(scores, key=lambda k: (scores[k], k == "en"))
    total = sum(scores.values())
    if total == 0:
        return LanguageGuess("en", "no-signal", 0.0)
    return LanguageGuess(best, "text", round(scores[best] / total, 2))


def detect_language(text: str = "", *, phone: str | None = None, country: str | None = None,
                    booking_language: str | None = None,
                    settings: Settings | None = None) -> LanguageGuess:
    """Pick the reply language for one guest. Never raises."""
    supported = list(settings.hotel.languages) if settings else list(LANGUAGES)
    default = supported[0] if supported else "en"

    if booking_language:
        code = str(booking_language).strip().lower()[:2]
        if code:
            return LanguageGuess(code, "booking", 1.0)

    if phone:
        n = normalise_phone(phone)
        for prefix in sorted(PHONE_PREFIXES, key=len, reverse=True):
            if n.startswith(prefix):
                return LanguageGuess(PHONE_PREFIXES[prefix], "phone", 0.8)

    if country:
        code = COUNTRY_CODES.get(str(country).strip().upper()[:2])
        if code:
            return LanguageGuess(code, "country", 0.7)

    guess = detect_from_text(text)
    if guess.confidence >= 0.3:
        return guess
    return LanguageGuess(default, "default", 0.0)


def format_date(iso: str, lang: str = "en", *, short: bool = False) -> str:
    """``2026-09-14`` -> ``Monday 14 September`` / ``14 septembre``."""
    from datetime import date, datetime
    try:
        d = datetime.fromisoformat(str(iso)).date()
    except ValueError:
        try:
            d = date.fromisoformat(str(iso)[:10])
        except ValueError:
            return str(iso)
    months = MONTHS.get(lang, MONTHS["en"]).split()
    if short:
        return f"{d.day} {months[d.month - 1]}"
    days = DAYS.get(lang, DAYS["en"]).split()
    return f"{days[d.weekday()]} {d.day} {months[d.month - 1]}"


def templates_dir() -> Path:
    return repo_root() / "templates"


def resolve_template(name: str, lang: str = "en", *, base: Path | None = None) -> Path:
    """Find ``<name>.<lang>.md``, falling back to ``.en.md`` then ``<name>.md``."""
    base = base or templates_dir()
    for candidate in (base / f"{name}.{lang}.md", base / f"{name}.en.md", base / f"{name}.md"):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"no template '{name}' for language '{lang}' in {base}. "
        f"Add {base}/{name}.{lang}.md or {base}/{name}.en.md.")


def render(template_path: Path | str, **vars: Any) -> dict:
    """Render a localised markdown template into ``{subject, body, meta}``.

    The file may carry YAML frontmatter (``subject:`` lives there). Both the
    subject and the body get ``{{var}}`` substitution.
    """
    path = Path(template_path)
    if not path.is_absolute():
        path = repo_root() / path
    if not path.exists():
        raise FileNotFoundError(f"template not found: {path}")
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    subject = render_string(str(meta.get("subject", "")), vars)
    return {"subject": subject, "body": render_string(body, vars).strip(), "meta": meta,
            "path": str(path)}


def render_localised(name: str, lang: str = "en", *, base: Path | None = None,
                     **vars: Any) -> dict:
    """:func:`resolve_template` + :func:`render` in one call."""
    return render(resolve_template(name, lang, base=base), **vars)


def strip_accents(text: str) -> str:
    """Accent-insensitive comparison helper (guest names, room names)."""
    return "".join(c for c in unicodedata.normalize("NFD", text or "")
                   if unicodedata.category(c) != "Mn")
