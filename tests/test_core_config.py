"""Tests for core.config — .env parsing, ${VAR} interpolation, load_settings().

Shared across every repo in the family (factory/scaffold/tests/). Runs from the
repo root: no network, no real credentials, an isolated tmp_path repo root via
AGENT_REPO_ROOT so it never touches this repo's own config/ or fixtures/.
"""

from __future__ import annotations

import pytest

from core.config import (AdapterConfig, ConfigError, load_settings, parse_dotenv,
                         repo_root)


def _write_repo(tmp_path, hotel_yaml: str = "", agent_yaml: str = "") -> None:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    if hotel_yaml:
        (tmp_path / "config" / "hotel.yaml").write_text(hotel_yaml, encoding="utf-8")
    if agent_yaml:
        (tmp_path / "config" / "agent.yaml").write_text(agent_yaml, encoding="utf-8")


def test_parse_dotenv_handles_quotes_export_and_comments():
    text = (
        "# a comment\n"
        "export FOO=bar  # trailing comment\n"
        'QUOTED="hello world"\n'
        "SINGLE='it work'\n"
        "\n"
        "NOVALUE=\n"
    )
    values = parse_dotenv(text)
    assert values["FOO"] == "bar"
    assert values["QUOTED"] == "hello world"
    assert values["SINGLE"] == "it work"
    assert values["NOVALUE"] == ""
    assert "# a comment" not in values


def test_repo_root_honours_agent_repo_root_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    assert repo_root() == tmp_path.resolve()


def test_load_settings_defaults_to_shadow_and_mock(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENT_MODE", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    _write_repo(tmp_path)  # no yaml files at all -> every default applies
    settings = load_settings()
    assert settings.mode == "shadow"
    assert settings.llm.provider == "mock"
    assert settings.hotel.name == "Your Hotel"
    assert settings.hotel.default_language == "en"
    assert settings.is_live is False


def test_env_var_interpolation_in_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("MY_HOTEL_NAME", "The Interpolated Inn")
    _write_repo(tmp_path, hotel_yaml=(
        "hotel:\n"
        "  name: \"${MY_HOTEL_NAME}\"\n"
        "  timezone: \"${MISSING_TZ:-UTC}\"\n"
    ))
    settings = load_settings()
    assert settings.hotel.name == "The Interpolated Inn"
    assert settings.hotel.timezone == "UTC"


def test_invalid_llm_provider_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    _write_repo(tmp_path, hotel_yaml="llm:\n  provider: carrier-pigeon\n")
    with pytest.raises(ConfigError):
        load_settings()


def test_agent_yaml_mode_can_only_be_stricter(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENT_MODE", raising=False)
    _write_repo(tmp_path, hotel_yaml="mode: live\n", agent_yaml="mode: shadow\n")
    settings = load_settings()
    assert settings.mode == "shadow"  # agent.yaml wins when it is stricter

    _write_repo(tmp_path, hotel_yaml="mode: shadow\n", agent_yaml="mode: live\n")
    settings2 = load_settings()
    assert settings2.mode == "shadow"  # hotel.yaml's shadow is never overridden looser


def test_cli_provider_override_beats_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    _write_repo(tmp_path, hotel_yaml="llm:\n  provider: interactive\n")
    settings = load_settings(provider="mock")
    assert settings.llm.provider == "mock"


def test_adapter_config_from_dict_and_string_forms():
    from_dict = AdapterConfig.from_dict({"adapter": "imap", "mailbox": "x@example.com"})
    assert from_dict.adapter == "imap"
    assert from_dict.get("mailbox") == "x@example.com"
    assert from_dict.get("missing", "fallback") == "fallback"

    from_string = AdapterConfig.from_dict("csv", default_adapter="mock")
    assert from_string.adapter == "csv"
    assert from_string.options == {}


def test_settings_agent_get_dotted_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    _write_repo(tmp_path, agent_yaml="triage:\n  limit: 20\n  nested:\n    deep: true\n")
    settings = load_settings()
    assert settings.agent_get("triage.limit") == 20
    assert settings.agent_get("triage.nested.deep") is True
    assert settings.agent_get("triage.missing", "fallback") == "fallback"


def test_demo_forces_mock_everything(tmp_path, monkeypatch):
    cfg = tmp_path / "config"; cfg.mkdir()
    (cfg / "hotel.yaml").write_text(
        "hotel:\n  name: Real Hotel\nmode: live\nsystems:\n  pms:\n    adapter: cloudbeds\n"
        "  email:\n    adapter: gmail\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("AGENT_MODE", "live")
    s = load_settings(demo=True)
    assert s.mode == "shadow" and s.llm.provider == "mock"
    assert s.systems.pms.adapter == "mock" and s.systems.email.adapter == "mock"


def test_yaml_bare_no_language_means_norwegian(tmp_path, monkeypatch):
    cfg = tmp_path / "config"; cfg.mkdir()
    (cfg / "hotel.yaml").write_text(
        "hotel:\n  name: Fjell Hotel\n  languages: [no, en]\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CONFIG_DIR", str(cfg))
    s = load_settings()
    assert s.hotel.languages == ["no", "en"]


def test_demo_reads_example_config_not_the_hotels_own(tmp_path, monkeypatch):
    cfg = tmp_path / "config"; cfg.mkdir()
    (cfg / "hotel.yaml").write_text("hotel:\n  name: Real Hotel\n", encoding="utf-8")
    (cfg / "hotel.example.yaml").write_text("hotel:\n  name: Hotel Aurora\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CONFIG_DIR", str(cfg))
    assert load_settings(demo=True).hotel.name == "Hotel Aurora"
    assert load_settings().hotel.name == "Real Hotel"


def test_launchd_snippet_follows_a_raw_cron_hour(tmp_path):
    from core.schedule import render
    out = render("launchd", command="tools/digest.py", cadence="0 6 * * *", slug="x", root=tmp_path)
    assert "<key>Hour</key><integer>6</integer>" in out
    weekly = render("launchd", command="tools/coach.py", cadence="30 3 * * 1", slug="x", root=tmp_path)
    assert "<key>Weekday</key><integer>1</integer>" in weekly and "<integer>30</integer>" in weekly


def test_monthly_cadence_renders_everywhere(tmp_path):
    from core.schedule import render
    cron = render("cron", command="tools/run.py --report", cadence="monthly", slug="x", root=tmp_path)
    assert "0 6 1 * *" in cron
    plist = render("launchd", command="tools/run.py --report", cadence="monthly", slug="x", root=tmp_path)
    assert "<key>Day</key><integer>1</integer>" in plist
    systemd = render("systemd", command="tools/run.py --report", cadence="monthly", slug="x", root=tmp_path)
    assert "OnCalendar=*-*-01 06:00:00" in systemd
