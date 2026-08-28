"""Tests for core.llm — the mock provider, schema validation and extract_json.

Shared across every repo (factory/scaffold/tests/). No network: only the `mock`
provider is exercised. Uses AGENT_REPO_ROOT + tmp_path so it never touches this
repo's own fixtures/.
"""

from __future__ import annotations

import json

import pytest

from core.llm import (LLMSchemaError, complete, extract_json, schema_example,
                      validate_schema)

SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["question", "complaint"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["intent", "confidence"],
    "additionalProperties": False,
}


def test_mock_without_schema_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    r1 = complete("greet", "hello there", provider="mock")
    r2 = complete("greet", "hello there", provider="mock")
    assert r1.provider == "mock"
    assert r1.text == r2.text  # same task+prompt -> same canned digest
    assert r1.data is None


def test_mock_with_schema_and_no_fixture_builds_a_valid_example(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    result = complete("classify", "some guest email", SCHEMA, provider="mock")
    assert result.data is not None
    assert not validate_schema(result.data, SCHEMA)  # no errors
    assert result.data["intent"] == "question"  # first enum value


def test_mock_prefers_a_matching_fixture_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    fx_dir = tmp_path / "fixtures" / "expected" / "classify"
    fx_dir.mkdir(parents=True)
    payload = {"intent": "complaint", "confidence": 0.92}
    (fx_dir / "guest-1.json").write_text(json.dumps(payload), encoding="utf-8")

    result = complete("classify", "the room was cold", SCHEMA, provider="mock",
                      fixture_id="guest-1")
    assert result.data == payload
    assert result.cached is True


def test_complete_raises_schema_error_when_fixture_does_not_match(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    fx_dir = tmp_path / "fixtures" / "expected" / "classify"
    fx_dir.mkdir(parents=True)
    (fx_dir / "bad-1.json").write_text(json.dumps({"intent": "not-an-option"}),
                                       encoding="utf-8")
    with pytest.raises(LLMSchemaError):
        complete("classify", "text", SCHEMA, provider="mock", fixture_id="bad-1")


def test_validate_schema_reports_missing_required_and_enum_errors():
    errors = validate_schema({"intent": "unknown"}, SCHEMA)
    assert any("confidence" in e for e in errors)
    assert any("unknown" in e for e in errors)
    assert validate_schema({"intent": "question", "confidence": 0.5}, SCHEMA) == []


def test_schema_example_picks_first_enum_and_zero_defaults():
    example = schema_example(SCHEMA)
    assert example["intent"] == "question"
    assert example["confidence"] == 0


def test_extract_json_handles_fenced_and_bare_json():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('here is the answer: {"a": 1} thanks') == {"a": 1}


def test_extract_json_raises_on_garbage():
    with pytest.raises(Exception):
        extract_json("not json at all")


def test_interactive_schemaless_answer_accepts_text_envelope(tmp_path, monkeypatch):
    """A schema-less interactive answer may be plain text OR {"text": ...} JSON."""
    import json as _json
    from core import llm as llm_mod
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    from core.llm import LLMPendingInteractive, _interactive
    try:
        _interactive("note", "sys", "user", None, "fx-1", "claude-opus-5")
    except LLMPendingInteractive as exc:
        exc.answer_path.write_text(_json.dumps({"text": "A clean sentence."}), encoding="utf-8")
        result = _interactive("note", "sys", "user", None, "fx-1", "claude-opus-5")
        assert result.text == "A clean sentence."
    else:  # pragma: no cover
        raise AssertionError("expected a pending prompt on first call")


def test_prompt_task_section_keeps_nested_headings(tmp_path, monkeypatch):
    from core import templates as tpl
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    pdir = tmp_path / "prompts"; pdir.mkdir(parents=True)
    (pdir / "demo_task.md").write_text(
        "---\ntask: demo_task\n---\n\n## System\nBe brief.\n\n## Task\n"
        "Read the list.\n\n## Open events\n- E-1\n- E-2\n\nAnswer now.\n",
        encoding="utf-8")
    parsed = tpl.load_prompt("demo_task")
    assert "Open events" in parsed.body and "E-2" in parsed.body
    assert "Answer now." in parsed.body
    assert parsed.system == "Be brief."


def test_interactive_answer_survives_a_dry_run(tmp_path, monkeypatch):
    """A dry run must not consume the answer file: dry-run state is discarded,
    so the next invocation needs to find the same answer again."""
    import json as _json
    from core import llm as llm_mod
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    from core.llm import LLMPendingInteractive, _interactive
    try:
        _interactive("note", "sys", "user", None, "fx-dry", "claude-opus-5")
    except LLMPendingInteractive as exc:
        exc.answer_path.write_text(_json.dumps({"text": "Answer."}), encoding="utf-8")
        first = _interactive("note", "sys", "user", None, "fx-dry", "claude-opus-5",
                             consume=False)
        assert first.text == "Answer."
        assert exc.answer_path.exists(), "dry run must leave the answer in place"
        second = _interactive("note", "sys", "user", None, "fx-dry", "claude-opus-5")
        assert second.text == "Answer." and not exc.answer_path.exists()
    else:  # pragma: no cover
        raise AssertionError("expected a pending prompt on first call")


def test_schema_example_respects_string_length_constraints():
    from core.llm import schema_example, validate_schema
    schema = {"type": "object", "required": ["language", "note"],
              "properties": {"language": {"type": "string", "minLength": 2, "maxLength": 2},
                             "note": {"type": "string", "minLength": 6}}}
    example = schema_example(schema)
    assert len(example["language"]) == 2 and len(example["note"]) >= 6
    assert validate_schema(example, schema) == []
