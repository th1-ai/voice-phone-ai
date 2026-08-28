"""core.config — load config/hotel.yaml + config/agent.yaml + .env into Settings.

Resolution order for each config file (first hit wins):

1. ``$AGENT_CONFIG_DIR/<name>.yaml``     explicit override directory
2. ``<repo>/config/<name>.yaml``         the hotel's filled-in copy
3. ``<repo>/config/<name>.example.yaml`` the shipped example (never edited)

Values may reference environment variables with ``${VAR}`` or ``${VAR:-default}``.
That is how a secret stays out of YAML and how the environment overrides a file.
A handful of top-level knobs also have direct env overrides so you can flip them
for one run without editing YAML:

    AGENT_MODE=shadow|live      LLM_PROVIDER=mock|interactive|claude-code|anthropic
    LLM_MODEL=<model id>        LLM_EFFORT=low|medium|high

Everything is plain dataclasses, so ``settings.systems.pms.adapter`` and
``settings.llm.provider`` autocomplete and typo loudly.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - requirements.txt pins pyyaml
    yaml = None  # type: ignore[assignment]

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

VALID_MODES = ("shadow", "live")
VALID_PROVIDERS = ("mock", "interactive", "claude-code", "anthropic")
DEFAULT_MODEL = "claude-opus-5"


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid. Message is user-facing."""


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------
def repo_root() -> Path:
    """Absolute path of the repo root (the folder that contains ``core/``).

    Override with ``AGENT_REPO_ROOT`` when running from somewhere unusual.
    """
    override = os.environ.get("AGENT_REPO_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """``<repo>/data`` — runtime state. Created on demand, gitignored."""
    d = repo_root() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sub_data_dir(name: str) -> Path:
    """``<repo>/data/<name>`` — e.g. ``logs``, ``pending``, ``exports``, ``imports``."""
    d = data_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------
# .env
# --------------------------------------------------------------------------
def parse_dotenv(text: str) -> dict[str, str]:
    """Parse ``.env`` text. Supports ``export KEY=value``, quotes and ``#`` comments."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        if key:
            out[key] = value
    return out


def load_dotenv(path: Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Read ``<repo>/.env`` into ``os.environ``. Real environment wins by default."""
    path = path or (repo_root() / ".env")
    if not path.exists():
        return {}
    values = parse_dotenv(path.read_text(encoding="utf-8"))
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return values


# --------------------------------------------------------------------------
# YAML loading + ${VAR} interpolation
# --------------------------------------------------------------------------
def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            return os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else "")
        return _ENV_PATTERN.sub(sub, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def config_path(name: str) -> Path | None:
    """Resolve ``<name>.yaml`` per the documented order. ``None`` if absent."""
    candidates: list[Path] = []
    override = os.environ.get("AGENT_CONFIG_DIR")
    if override:
        candidates.append(Path(override) / f"{name}.yaml")
    cfg = repo_root() / "config"
    candidates += [cfg / f"{name}.yaml", cfg / f"{name}.example.yaml"]
    for c in candidates:
        if c.exists():
            return c
    return None


def load_yaml(name: str) -> dict:
    """Load one config file with ``${VAR}`` interpolation. Missing file -> ``{}``."""
    path = config_path(name)
    if path is None:
        return {}
    if yaml is None:
        raise ConfigError("pyyaml is not installed. Run: make setup")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:  # indentation, tabs, stray colons ...
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        raise ConfigError(
            f"{path}{where}: {problem}. Check the indentation (two spaces, no tabs) "
            f"and compare with the .example.yaml next to it.") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return _interpolate(raw)


# --------------------------------------------------------------------------
# typed settings
# --------------------------------------------------------------------------
@dataclass
class AdapterConfig:
    """One entry under ``systems:`` — which adapter, plus its own options."""

    adapter: str = "mock"
    options: dict = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.options[key]

    @classmethod
    def from_dict(cls, raw: Any, default_adapter: str = "mock") -> "AdapterConfig":
        if not isinstance(raw, dict):
            return cls(adapter=str(raw or default_adapter), options={})
        opts = {k: v for k, v in raw.items() if k != "adapter"}
        return cls(adapter=str(raw.get("adapter") or default_adapter), options=opts)


@dataclass
class HotelConfig:
    name: str = "Your Hotel"
    legal_name: str = ""
    timezone: str = "UTC"
    currency: str = "EUR"
    languages: list[str] = field(default_factory=lambda: ["en"])
    rooms: int = 0
    address: str = ""
    website: str = ""
    phone: str = ""
    email: str = ""

    @property
    def default_language(self) -> str:
        """First entry in ``languages`` — the reply language when undetectable."""
        return self.languages[0] if self.languages else "en"


@dataclass
class ContactsConfig:
    manager: dict = field(default_factory=dict)
    escalation_email: str = ""


@dataclass
class SystemsConfig:
    pms: AdapterConfig = field(default_factory=lambda: AdapterConfig("mock"))
    email: AdapterConfig = field(default_factory=lambda: AdapterConfig("mock"))
    messaging: AdapterConfig = field(default_factory=lambda: AdapterConfig("mock"))
    sheets: AdapterConfig = field(default_factory=lambda: AdapterConfig("csv"))


@dataclass
class LLMConfig:
    provider: str = "mock"
    model: str = DEFAULT_MODEL
    effort: str = "medium"
    max_tokens: int = 4000


@dataclass
class ReviewConfig:
    require_approval_for: list[str] = field(
        default_factory=lambda: ["send_email", "send_message", "pms_write", "payment", "publish"]
    )
    auto_approve_after_days: int | None = None
    digest_hour: int = 8


@dataclass
class PrivacyConfig:
    redact_cards: bool = True
    retention_days: int = 365


@dataclass
class Settings:
    """Everything a tool needs, already merged and typed."""

    hotel: HotelConfig = field(default_factory=HotelConfig)
    contacts: ContactsConfig = field(default_factory=ContactsConfig)
    systems: SystemsConfig = field(default_factory=SystemsConfig)
    mode: str = "shadow"
    llm: LLMConfig = field(default_factory=LLMConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    agent: dict = field(default_factory=dict)
    root: Path = field(default_factory=repo_root)
    dry_run: bool = False
    #: True only for `make demo` (load_settings(demo=True)): sample data, never the hotel's.
    demo: bool = False
    raw: dict = field(default_factory=dict)

    # -- convenience ------------------------------------------------------
    @property
    def is_live(self) -> bool:
        return self.mode == "live" and not self.dry_run

    def agent_get(self, path: str, default: Any = None) -> Any:
        """Dotted lookup into ``config/agent.yaml``, e.g. ``agent_get("triage.limit", 20)``."""
        node: Any = self.agent
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def db_path(self) -> Path:
        return data_dir() / "agent.db"


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_settings(*, provider: str | None = None, mode: str | None = None,
                  dry_run: bool = False, demo: bool = False) -> Settings:
    """Build :class:`Settings` from ``.env`` + ``hotel.yaml`` + ``agent.yaml``.

    ``provider`` / ``mode`` are the CLI overrides (``--provider``, ``--mode``);
    they beat both YAML and the environment. ``demo=True`` is what
    ``tools/demo.py`` uses: mock provider, shadow mode and the ``mock`` adapter
    for every system, whatever the hotel has configured, so a demo can never
    read a real mailbox or PMS.
    """
    load_dotenv()
    hotel_raw = load_yaml("hotel")
    agent_raw = load_yaml("agent")
    if demo:
        # Sample data only: the demo runs on the shipped example config, so a
        # hotel's own edits (room ladder, rules, identity) can never change
        # what `make demo` shows — and the demo can never read their systems.
        example_hotel = load_yaml("hotel.example")
        example_agent = load_yaml("agent.example")
        if example_hotel:
            hotel_raw = example_hotel
        if example_agent:
            agent_raw = example_agent
        provider, mode = "mock", "shadow"
        systems_raw = hotel_raw.get("systems")
        if not isinstance(systems_raw, dict):
            systems_raw = {}
        hotel_raw["systems"] = systems_raw
        for family in ("pms", "email", "messaging", "sheets"):
            fam = systems_raw.get(family)
            fam = dict(fam) if isinstance(fam, dict) else {}
            fam["adapter"] = "mock"
            systems_raw[family] = fam

    h = hotel_raw.get("hotel") or {}
    langs = h.get("languages") or ["en"]
    # YAML 1.1 reads a bare `no` as boolean False — and `no` is Norwegian.
    # A hotel typing `languages: [no, en]` means Norwegian, not "false".
    langs = ["no" if x is False else x for x in langs]
    if isinstance(langs, str):
        langs = [s.strip() for s in langs.split(",") if s.strip()]

    hotel = HotelConfig(
        name=str(h.get("name") or "Your Hotel"),
        legal_name=str(h.get("legal_name") or ""),
        timezone=str(h.get("timezone") or "UTC"),
        currency=str(h.get("currency") or "EUR"),
        languages=[str(x).lower() for x in langs],
        rooms=_as_int(h.get("rooms"), 0),
        address=str(h.get("address") or ""),
        website=str(h.get("website") or ""),
        phone=str(h.get("phone") or ""),
        email=str(h.get("email") or ""),
    )

    c = hotel_raw.get("contacts") or {}
    contacts = ContactsConfig(
        manager=dict(c.get("manager") or {}),
        escalation_email=str(c.get("escalation_email") or ""),
    )

    s = hotel_raw.get("systems") or {}
    systems = SystemsConfig(
        pms=AdapterConfig.from_dict(s.get("pms"), "mock"),
        email=AdapterConfig.from_dict(s.get("email"), "mock"),
        messaging=AdapterConfig.from_dict(s.get("messaging"), "mock"),
        sheets=AdapterConfig.from_dict(s.get("sheets"), "csv"),
    )

    lraw = {**(hotel_raw.get("llm") or {}), **(agent_raw.get("llm") or {})}
    llm = LLMConfig(
        provider=str(provider or os.environ.get("LLM_PROVIDER") or lraw.get("provider") or "mock"),
        model=str(os.environ.get("LLM_MODEL") or lraw.get("model") or DEFAULT_MODEL),
        effort=str(os.environ.get("LLM_EFFORT") or lraw.get("effort") or "medium"),
        max_tokens=_as_int(lraw.get("max_tokens"), 4000),
    )
    if llm.provider not in VALID_PROVIDERS:
        raise ConfigError(
            f"llm.provider must be one of {', '.join(VALID_PROVIDERS)} (got '{llm.provider}')"
        )

    rraw = {**(hotel_raw.get("review") or {}), **(agent_raw.get("review") or {})}
    review = ReviewConfig(
        require_approval_for=list(
            rraw.get("require_approval_for")
            or ["send_email", "send_message", "pms_write", "payment", "publish"]
        ),
        auto_approve_after_days=(
            None if rraw.get("auto_approve_after_days") in (None, "", "null")
            else _as_int(rraw.get("auto_approve_after_days"), 0)
        ),
        digest_hour=_as_int(rraw.get("digest_hour"), 8),
    )

    praw = hotel_raw.get("privacy") or {}
    privacy = PrivacyConfig(
        redact_cards=bool(praw.get("redact_cards", True)),
        retention_days=_as_int(praw.get("retention_days"), 365),
    )

    resolved_mode = str(mode or os.environ.get("AGENT_MODE") or hotel_raw.get("mode") or "shadow")
    # agent.yaml may only ever be STRICTER than hotel.yaml, never looser.
    if str(agent_raw.get("mode") or "") == "shadow":
        resolved_mode = "shadow"
    if resolved_mode not in VALID_MODES:
        raise ConfigError(f"mode must be 'shadow' or 'live' (got '{resolved_mode}')")

    return Settings(
        hotel=hotel, contacts=contacts, systems=systems, mode=resolved_mode,
        llm=llm, review=review, privacy=privacy,
        agent={k: v for k, v in agent_raw.items() if k not in ("llm", "review")},
        root=repo_root(), dry_run=dry_run, demo=demo,
        raw={"hotel": hotel_raw, "agent": agent_raw},
    )
