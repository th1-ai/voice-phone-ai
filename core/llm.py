"""core.llm — the one place the agent talks to a model.

Nothing else in a template calls an LLM. Tools call :func:`complete`, pass a JSON
schema, and get back a validated object or an exception they can act on.

Four providers, chosen by ``llm.provider`` in config or ``--provider``:

``mock``         canned answers from ``fixtures/expected/<task>/<id>.json``, or a
                 deterministic answer built from the schema. No credentials, no
                 network. This is what ``make demo`` and the tests run on.
``interactive``  writes the prompt to ``data/pending/<id>.prompt.md`` and stops.
                 The hotel's own Claude Code session reads it, writes
                 ``data/pending/<id>.answer.json``, and re-runs the command. Zero
                 extra model calls, and you see exactly what the agent asked.
``claude-code``  shells out to the ``claude`` CLI in headless mode, on the hotel's
                 own Claude Code login. Good for a Mac or a small box on cron.
``anthropic``    the Anthropic Python SDK with the hotel's own API key. Use this
                 for server deployments and real volume.

Failure is typed on purpose, so ``tools/run.py`` can back off instead of looping:
:class:`LLMBudgetExhausted` (rate limit / billing), :class:`LLMAuthError`,
:class:`LLMProviderUnavailable`, :class:`LLMSchemaError` (model answered off-schema
— queue the item as ``needs_human`` rather than guess), :class:`LLMRefusal`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.config import Settings, repo_root, sub_data_dir

PENDING_EXIT_CODE = 3
DEFAULT_MAX_TOKENS = 4000
VALID_EFFORTS = ("low", "medium", "high")


# --------------------------------------------------------------------------
# result + errors
# --------------------------------------------------------------------------
@dataclass
class LLMResult:
    """What every provider returns. ``data`` is populated when a schema was given."""

    text: str
    data: dict | None = None
    provider: str = ""
    model: str = ""
    usage: dict = field(default_factory=dict)
    cached: bool = False
    refusal: bool = False
    cost_usd: float = 0.0
    raw: dict = field(default_factory=dict)


class LLMError(RuntimeError):
    """Base class. Every message is written to be shown to the hotel."""


class LLMSchemaError(LLMError):
    """The model's answer did not match the schema. Do not guess — escalate."""


class LLMBudgetExhausted(LLMError):
    """Rate limited, out of credit, or overloaded. Back off and retry later."""


class LLMAuthError(LLMError):
    """Not logged in / bad API key. A human has to fix the credentials."""


class LLMProviderUnavailable(LLMError):
    """The provider cannot run here at all (binary missing, SDK not installed)."""


class LLMRefusal(LLMError):
    """The model declined to answer. Route the item to a human."""


class LLMPendingInteractive(Exception):
    """The interactive provider parked a prompt and is waiting for an answer.

    Deliberately NOT an :class:`LLMError`: it is a pause, not a failure, and a
    broad ``except LLMError`` around a model call must never swallow it (two
    agents did exactly that and silently fell back to canned text).
    ``tools/run.py`` catches this, prints the instructions and exits with code 3.
    """

    def __init__(self, pending_id: str, prompt_path: Path, schema_path: Path | None,
                 answer_path: Path) -> None:
        super().__init__(
            f"waiting for an answer to prompt {pending_id}.\n"
            f"  prompt:  {prompt_path}\n"
            + (f"  schema:  {schema_path}\n" if schema_path else "")
            + f"  answer:  {answer_path}\n"
            f"  Read the prompt, write your answer as JSON to the answer path, "
            f"then run the same command again.")
        self.pending_id = pending_id
        self.prompt_path = prompt_path
        self.schema_path = schema_path
        self.answer_path = answer_path


# --------------------------------------------------------------------------
# a small JSON-schema validator (type / required / enum / properties / items /
# additionalProperties / minimum / maximum / anyOf) — enough for agent outputs
# --------------------------------------------------------------------------
_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,), "array": (list,), "string": (str,), "number": (int, float),
    "integer": (int,), "boolean": (bool,), "null": (type(None),),
}


def validate_schema(data: Any, schema: dict, path: str = "$") -> list[str]:
    """Validate ``data`` against ``schema``. Returns a list of human-readable errors."""
    errors: list[str] = []
    if not isinstance(schema, dict):
        return errors

    if "anyOf" in schema:
        for option in schema["anyOf"]:
            if not validate_schema(data, option, path):
                return []
        return [f"{path}: does not match any of the allowed shapes"]

    expected = schema.get("type")
    if expected:
        types = [expected] if isinstance(expected, str) else list(expected)
        ok = False
        for t in types:
            allowed = _TYPES.get(t, ())
            if t == "number" and isinstance(data, bool):
                continue
            if t == "integer" and isinstance(data, bool):
                continue
            if allowed and isinstance(data, allowed):
                ok = True
                break
        if not ok:
            errors.append(f"{path}: expected {'/'.join(types)}, got "
                          f"{type(data).__name__}")
            return errors

    if "enum" in schema and data not in schema["enum"]:
        errors.append(f"{path}: {data!r} is not one of {schema['enum']}")

    if isinstance(data, (int, float)) and not isinstance(data, bool):
        if "minimum" in schema and data < schema["minimum"]:
            errors.append(f"{path}: {data} < minimum {schema['minimum']}")
        if "maximum" in schema and data > schema["maximum"]:
            errors.append(f"{path}: {data} > maximum {schema['maximum']}")

    if isinstance(data, str):
        if "minLength" in schema and len(data) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(data) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength {schema['maxLength']}")

    if isinstance(data, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in data:
                errors.append(f"{path}: missing required property '{key}'")
        for key, value in data.items():
            if key in props:
                errors += validate_schema(value, props[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property '{key}'")

    if isinstance(data, list) and isinstance(schema.get("items"), dict):
        for i, entry in enumerate(data):
            errors += validate_schema(entry, schema["items"], f"{path}[{i}]")

    return errors


def schema_example(schema: dict) -> Any:
    """Build the smallest value that satisfies ``schema``.

    Used by the ``mock`` provider when no fixture matches: first enum value,
    ``"mock"`` for free-text strings, ``0`` for numbers, ``[]`` for arrays.
    """
    if not isinstance(schema, dict):
        return None
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    if "default" in schema:
        return schema["default"]
    if "anyOf" in schema and schema["anyOf"]:
        return schema_example(schema["anyOf"][0])
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "string")
    if kind == "object":
        props = schema.get("properties") or {}
        required = schema.get("required") or list(props)
        return {k: schema_example(props[k]) for k in required if k in props}
    if kind == "array":
        item = schema.get("items")
        return [schema_example(item)] if schema.get("minItems") and isinstance(item, dict) else []
    if kind == "boolean":
        return False
    if kind in ("number", "integer"):
        return schema.get("minimum", 0)
    if kind == "null":
        return None
    text = "mock"
    lo, hi = schema.get("minLength"), schema.get("maxLength")
    if isinstance(hi, int) and len(text) > hi:
        text = text[:hi] or "x"
    if isinstance(lo, int) and len(text) < lo:
        text = (text * (lo // max(len(text), 1) + 1))[:lo]
    return text
def extract_json(text: str) -> Any:
    """Pull a JSON object out of a model reply that may be wrapped in prose/fences."""
    if text is None:
        raise LLMSchemaError("empty response")
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.S)
    if fence:
        candidate = fence.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    start = min([i for i in (candidate.find("{"), candidate.find("[")) if i >= 0] or [-1])
    if start >= 0:
        for end in range(len(candidate), start, -1):
            try:
                return json.loads(candidate[start:end])
            except json.JSONDecodeError:
                continue
    raise LLMSchemaError("response was not JSON: " + candidate[:300])


# --------------------------------------------------------------------------
# provider: mock
# --------------------------------------------------------------------------
def _fixture_path(task: str, fixture_id: str | None) -> Path | None:
    if not fixture_id:
        return None
    path = repo_root() / "fixtures" / "expected" / task / f"{fixture_id}.json"
    return path if path.exists() else None


def _mock(task: str, prompt: str, schema: dict | None, fixture_id: str | None,
          model: str) -> LLMResult:
    path = _fixture_path(task, fixture_id)
    if path is not None:
        data = json.loads(path.read_text(encoding="utf-8"))
        return LLMResult(text=json.dumps(data, ensure_ascii=False), data=data,
                         provider="mock", model=model, cached=True,
                         usage={"input_tokens": 0, "output_tokens": 0})
    if schema:
        data = schema_example(schema)
        return LLMResult(text=json.dumps(data, ensure_ascii=False), data=data,
                         provider="mock", model=model,
                         usage={"input_tokens": 0, "output_tokens": 0})
    digest = hashlib.sha256(f"{task}\n{prompt}".encode()).hexdigest()[:8]
    return LLMResult(text=f"[mock:{task}:{digest}]", provider="mock", model=model,
                     usage={"input_tokens": 0, "output_tokens": 0})


# --------------------------------------------------------------------------
# provider: interactive
# --------------------------------------------------------------------------
def _pending_id(task: str, system: str, user: str, fixture_id: str | None) -> str:
    if fixture_id:
        return f"{task}-{re.sub(r'[^A-Za-z0-9_-]+', '-', str(fixture_id))}"
    digest = hashlib.sha256(f"{task}\n{system}\n{user}".encode()).hexdigest()[:10]
    return f"{task}-{digest}"


def _interactive(task: str, system: str, user: str, schema: dict | None,
                 fixture_id: str | None, model: str, *, consume: bool = True) -> LLMResult:
    pending = sub_data_dir("pending")
    pid = _pending_id(task, system, user, fixture_id)
    prompt_path = pending / f"{pid}.prompt.md"
    schema_path = pending / f"{pid}.schema.json" if schema else None
    answer_path = pending / f"{pid}.answer.json"

    if answer_path.exists():
        raw = answer_path.read_text(encoding="utf-8")
        data = extract_json(raw) if schema else None
        if schema:
            errors = validate_schema(data, schema)
            if errors:
                raise LLMSchemaError(
                    f"{answer_path} does not match the schema:\n  - " + "\n  - ".join(errors))
        text = raw.strip()
        if not schema:
            # Schema-less tasks want prose. Accept either plain text or the
            # documented {"text": "..."} envelope — never hand a raw JSON blob
            # to code that expects a sentence.
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
                    text = parsed["text"].strip()
            except ValueError:
                pass
        if consume:
            answer_path.rename(answer_path.with_suffix(".json.used"))
            for leftover in (prompt_path, schema_path):
                if leftover is not None and leftover.exists():
                    leftover.unlink()
        # On a --dry-run pass the answer file is left in place: a dry run keeps
        # no state, so consuming the answer would strand the NEXT invocation
        # (the prompt id is deterministic, so re-reading is safe).
        return LLMResult(text=text, data=data if isinstance(data, dict) else None,
                         provider="interactive", model=model, cached=True,
                         usage={"input_tokens": 0, "output_tokens": 0})

    body = [f"# Pending prompt: {task}", "",
            f"Answer this and write the result to `{answer_path.name}` in this folder"
            + (" as JSON matching the schema below." if schema else " as JSON with a "
               "single key `text`."),
            "", "## System", "", system or "(none)", "", "## Task", "", user]
    if schema:
        body += ["", "## Response schema", "", "```json",
                 json.dumps(schema, indent=2, ensure_ascii=False), "```"]
    prompt_path.write_text("\n".join(body), encoding="utf-8")
    if schema_path is not None and schema is not None:
        schema_path.write_text(json.dumps(schema, indent=2, ensure_ascii=False),
                               encoding="utf-8")
    raise LLMPendingInteractive(pid, prompt_path, schema_path, answer_path)


# --------------------------------------------------------------------------
# provider: claude-code (headless CLI on the hotel's own subscription)
# --------------------------------------------------------------------------
_TERMINAL_BUDGET = {"rate_limit", "billing_error", "overloaded"}


def _claude_code(task: str, system: str, user: str, schema: dict | None, model: str,
                 effort: str, timeout: int = 600) -> LLMResult:
    binary = shutil.which("claude")
    if not binary:
        raise LLMProviderUnavailable(
            "the `claude` command is not on your PATH.\n"
            "  Install Claude Code (https://claude.com/claude-code) and run `claude` once "
            "to log in,\n  or switch to another provider: set llm.provider to 'anthropic' "
            "(needs ANTHROPIC_API_KEY)\n  or 'interactive' (answer prompts in your own "
            "Claude session, no extra cost).")

    cmd = [binary, "-p", "--output-format", "json",
           "--allowedTools", "", "--permission-mode", "dontAsk",
           "--model", model, "--effort", effort]
    system_file: Path | None = None
    if schema:
        cmd += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
    if system:
        system_file = sub_data_dir("pending") / f".system-{task}.md"
        system_file.write_text(system, encoding="utf-8")
        cmd += ["--system-prompt-file", str(system_file)]

    try:
        proc = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise LLMProviderUnavailable(f"`claude` timed out after {timeout}s") from exc
    finally:
        if system_file is not None and system_file.exists():
            system_file.unlink()

    try:
        envelope = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        envelope = {}

    terminal = str(envelope.get("terminal_reason") or "")
    if terminal in _TERMINAL_BUDGET:
        raise LLMBudgetExhausted(
            f"Claude Code stopped: {terminal}. Your subscription is rate limited or out of "
            "credit.\n  Wait and re-run, or switch llm.provider to 'anthropic' with your own "
            "API key for higher volume.")
    if terminal == "authentication_failed":
        raise LLMAuthError(
            "Claude Code is not authenticated. Run `claude` once and log in, or set "
            "CLAUDE_CODE_OAUTH_TOKEN on a server.")
    if envelope.get("is_error") or proc.returncode != 0:
        detail = (envelope.get("result") or proc.stderr or "unknown error").strip()[:400]
        status = envelope.get("api_error_status")
        if status and int(status) in (401, 403):
            raise LLMAuthError(f"Claude Code auth error {status}: {detail}")
        if status and int(status) == 429:
            raise LLMBudgetExhausted(f"Claude Code rate limited: {detail}")
        raise LLMError(f"claude-code failed: {detail}")

    text = str(envelope.get("result") or "")
    data = envelope.get("structured_output")
    if schema and data is None and text:
        data = extract_json(text)
    usage = envelope.get("usage") or {}
    return LLMResult(
        text=text, data=data if isinstance(data, dict) else None, provider="claude-code",
        model=model, usage=usage, cost_usd=float(envelope.get("total_cost_usd") or 0.0),
        refusal=str(envelope.get("stop_reason") or "") == "refusal",
        raw={"session_id": envelope.get("session_id"), "num_turns": envelope.get("num_turns")})


# --------------------------------------------------------------------------
# provider: anthropic (the hotel's own API key)
# --------------------------------------------------------------------------
def _anthropic(task: str, system: str, user: str, schema: dict | None, model: str,
               effort: str, max_tokens: int, retries: int = 3) -> LLMResult:
    try:
        import anthropic
    except ImportError as exc:
        raise LLMProviderUnavailable(
            "the `anthropic` package is not installed. Run: pip install anthropic\n"
            "  (or switch llm.provider to 'claude-code' / 'interactive')") from exc

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise LLMAuthError(
            "ANTHROPIC_API_KEY is not set. Put it in .env (see .env.example), or switch "
            "llm.provider to 'claude-code' to use your Claude Code login instead.")

    client = anthropic.Anthropic()
    kwargs: dict[str, Any] = {
        "model": model, "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user}],
    }
    if system:
        kwargs["system"] = system
    output_config: dict[str, Any] = {"effort": effort}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    kwargs["output_config"] = output_config

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = client.messages.create(**kwargs)
            break
        except TypeError:
            # Older SDK without output_config: retry once without it.
            kwargs.pop("output_config", None)
            response = client.messages.create(**kwargs)
            break
        except anthropic.RateLimitError as exc:
            raise LLMBudgetExhausted(
                "Anthropic rate limit or quota reached. Wait, raise your limit, or run "
                "fewer items per pass (--limit).") from exc
        except anthropic.APIStatusError as exc:
            status = getattr(exc, "status_code", 0) or 0
            if status in (401, 403):
                raise LLMAuthError(f"Anthropic rejected the API key ({status}).") from exc
            if status == 402:
                raise LLMBudgetExhausted("Anthropic reports no remaining credit.") from exc
            if status >= 500 and attempt < retries - 1:
                last_error = exc
                time.sleep(2 ** attempt)
                continue
            raise LLMError(f"Anthropic API error {status}: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            if attempt < retries - 1:
                last_error = exc
                time.sleep(2 ** attempt)
                continue
            raise LLMProviderUnavailable(
                "could not reach the Anthropic API. Check the machine's internet "
                "connection.") from exc
    else:  # pragma: no cover - loop always breaks or raises
        raise LLMError(f"Anthropic API unavailable: {last_error}")

    refusal = getattr(response, "stop_reason", "") == "refusal"
    parts = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    text = "\n".join(parts)
    if refusal:
        raise LLMRefusal(
            f"the model declined to answer the '{task}' prompt. This item needs a human.")
    data = extract_json(text) if schema else None
    usage_obj = getattr(response, "usage", None)
    usage = {
        "input_tokens": getattr(usage_obj, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage_obj, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(usage_obj, "cache_read_input_tokens", 0) or 0,
    }
    return LLMResult(text=text, data=data if isinstance(data, dict) else None,
                     provider="anthropic", model=model, usage=usage, refusal=False,
                     cached=bool(usage.get("cache_read_input_tokens")))


# --------------------------------------------------------------------------
# the public entry point
# --------------------------------------------------------------------------
def complete(task: str, prompt: Any, schema: dict | None = None, *,
             system: str | None = None, effort: str = "medium",
             max_tokens: int = DEFAULT_MAX_TOKENS, settings: Settings | None = None,
             provider: str | None = None, store: Any = None, item_id: str | None = None,
             fixture_id: str | None = None, meta: dict | None = None) -> LLMResult:
    """Run one reasoning step and return a validated :class:`LLMResult`.

    ``prompt`` is either a string or a :class:`core.templates.RenderedPrompt`, in
    which case its ``system`` and ``fixture_id`` are used unless overridden.
    ``schema`` is a JSON schema; when given, the answer is validated against it
    and :class:`LLMSchemaError` is raised if it does not fit.

    Usage is recorded to the ``events`` table when a ``store`` is passed, so
    ``tools/report.py`` can show what the agent spent.
    """
    system_text = system or ""
    user_text = prompt
    if hasattr(prompt, "user"):  # RenderedPrompt
        user_text = prompt.user
        system_text = system if system is not None else getattr(prompt, "system", "")
        fixture_id = fixture_id or getattr(prompt, "fixture_id", None)
    user_text = str(user_text)
    fixture_id = fixture_id or (meta or {}).get("fixture_id")

    name = provider or (settings.llm.provider if settings else "mock")
    model = settings.llm.model if settings else "claude-opus-5"
    if settings and effort == "medium":
        effort = settings.llm.effort
    if effort not in VALID_EFFORTS:
        effort = "medium"
    if settings and max_tokens == DEFAULT_MAX_TOKENS:
        max_tokens = settings.llm.max_tokens

    started = time.monotonic()
    if name == "mock":
        result = _mock(task, user_text, schema, fixture_id, model)
    elif name == "interactive":
        consume = not bool(getattr(settings, "dry_run", False))
        result = _interactive(task, system_text, user_text, schema, fixture_id, model,
                              consume=consume)
    elif name == "claude-code":
        result = _claude_code(task, system_text, user_text, schema, model, effort)
    elif name == "anthropic":
        result = _anthropic(task, system_text, user_text, schema, model, effort, max_tokens)
    else:
        raise LLMProviderUnavailable(
            f"unknown provider '{name}'. Use mock, interactive, claude-code or anthropic.")

    if schema:
        if result.data is None:
            raise LLMSchemaError(
                f"task '{task}': the model returned no structured answer.\n"
                f"  Got: {result.text[:300]}")
        errors = validate_schema(result.data, schema)
        if errors:
            raise LLMSchemaError(
                f"task '{task}': the answer does not match the schema:\n  - "
                + "\n  - ".join(errors[:8]))

    if store is not None:
        try:
            store.record_event(item_id, "agent", "llm_call", {
                "task": task, "provider": result.provider, "model": result.model,
                "usage": result.usage, "cost_usd": result.cost_usd,
                "cached": result.cached, "seconds": round(time.monotonic() - started, 2),
            })
        except Exception:  # noqa: BLE001 - never fail a run because logging failed
            pass
    return result
