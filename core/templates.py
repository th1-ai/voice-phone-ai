"""core.templates — ``{{var}}`` rendering and prompt assembly.

Two things live here.

**Text templates.** :func:`render_string` fills ``{{name}}`` placeholders from a
dict. Unknown placeholders are left visible so the hotel spots a typo instead of
sending an email with a hole in it.

**Prompts.** Every reasoning step reads its prompt from ``prompts/<task>.md`` so
a hotel can edit how the agent thinks without touching Python. The same prompt is
handed to all four providers, which is what makes the ``interactive`` provider a
faithful preview of what ``claude-code`` or ``anthropic`` would see.

A prompt file looks like this::

    ---
    knowledge: [property.md, faq.md]     # optional, prepended to the system block
    fixture_id: welcome-01               # optional, used by the mock provider
    ---
    ## System
    You are the front desk assistant for {{hotel_name}}. ...

    ## Task
    Classify the message below into one of: {{intents}}.

:func:`build_prompt` assembles it in cache-friendly order — stable hotel facts
and knowledge first, then the task template, then the volatile item last — so a
provider with prompt caching gets a long, unchanging prefix.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.config import Settings, repo_root

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")


class TemplateError(RuntimeError):
    """Raised for a missing template file or, in strict mode, a missing variable."""


def render_string(text: str, mapping: dict | None = None, *, strict: bool = False,
                  **kwargs: Any) -> str:
    """Fill ``{{name}}`` placeholders. Dotted names index into nested dicts."""
    values: dict[str, Any] = {**(mapping or {}), **kwargs}

    def lookup(path: str) -> Any:
        if path in values:
            return values[path]
        node: Any = values
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return None
        return node

    missing: list[str] = []

    def sub(m: "re.Match[str]") -> str:
        found = lookup(m.group(1))
        if found is None:
            missing.append(m.group(1))
            return m.group(0)
        return str(found)

    out = _VAR.sub(sub, text)
    if strict and missing:
        raise TemplateError(f"template variables not supplied: {', '.join(sorted(set(missing)))}")
    return out


def render_file(path: Path | str, mapping: dict | None = None, *, strict: bool = False,
                **kwargs: Any) -> str:
    """Render a template file from disk. Path may be relative to the repo root."""
    p = Path(path)
    if not p.is_absolute():
        p = repo_root() / p
    if not p.exists():
        raise TemplateError(f"template not found: {p}")
    return render_string(p.read_text(encoding="utf-8"), mapping, strict=strict, **kwargs)


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Split an optional ``---`` YAML frontmatter block from a markdown body."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    if yaml is not None:
        meta = yaml.safe_load(parts[1]) or {}
    return (meta if isinstance(meta, dict) else {}), parts[2].lstrip("\n")


def _section(body: str, heading: str) -> str | None:
    """Pull one ``## Heading`` section out of a markdown body."""
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.I | re.M)
    match = pattern.search(body)
    if not match:
        return None
    rest = body[match.end():]
    # Only the contract's own top-level sections end a section. A prompt is
    # free to use `## Anything` INSIDE its Task text (an "Open events" list,
    # a rubric ...) without that content being silently deleted.
    nxt = re.search(r"^##\s+(System|Task)\s*$", rest, re.I | re.M)
    return (rest[:nxt.start()] if nxt else rest).strip()


@dataclass
class PromptTemplate:
    """A parsed ``prompts/<task>.md``."""

    task: str
    meta: dict = field(default_factory=dict)
    system: str = ""
    body: str = ""
    path: Path | None = None


@dataclass
class RenderedPrompt:
    """What goes to the model: a stable ``system`` and a volatile ``user``."""

    system: str
    user: str
    fixture_id: str | None = None
    meta: dict = field(default_factory=dict)


def prompts_dir() -> Path:
    return repo_root() / "prompts"


def load_prompt(task: str) -> PromptTemplate:
    """Load ``prompts/<task>.md``. Raises with a path a hotel can act on."""
    path = prompts_dir() / f"{task}.md"
    if not path.exists():
        raise TemplateError(
            f"no prompt for task '{task}'. Expected {path}. "
            f"Prompts live in prompts/ and are plain markdown you can edit.")
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    system = _section(body, "System") or ""
    task_body = _section(body, "Task")
    if task_body is None:
        task_body = body if not system else ""
    return PromptTemplate(task=task, meta=meta, system=system.strip(),
                          body=task_body.strip(), path=path)


def load_knowledge(names: list[str] | None) -> str:
    """Concatenate files from ``knowledge/``, skipping any that are absent."""
    if not names:
        return ""
    chunks = []
    base = repo_root() / "knowledge"
    for name in names:
        path = base / name
        if not path.exists() and path.with_suffix(".example.md").exists():
            path = path.with_suffix(".example.md")
        if path.exists():
            chunks.append(f"### {path.stem}\n{path.read_text(encoding='utf-8').strip()}")
    return "\n\n".join(chunks)


def hotel_block(settings: Settings) -> str:
    """The stable hotel facts every prompt starts with."""
    h = settings.hotel
    lines = [
        "### Property",
        f"- Name: {h.name}",
        f"- Timezone: {h.timezone}",
        f"- Currency: {h.currency}",
        f"- Languages: {', '.join(h.languages)} (default: {h.default_language})",
    ]
    if h.rooms:
        lines.append(f"- Rooms: {h.rooms}")
    if h.website:
        lines.append(f"- Website: {h.website}")
    if h.phone:
        lines.append(f"- Phone: {h.phone}")
    if h.email:
        lines.append(f"- Email: {h.email}")
    lines.append(f"- Agent mode: {settings.mode} "
                 f"({'drafts only, nothing is sent' if settings.mode == 'shadow' else 'live'})")
    return "\n".join(lines)


def build_prompt(task: str, *, settings: Settings | None = None, item: Any = None,
                 knowledge: list[str] | None = None, fixture_id: str | None = None,
                 **vars: Any) -> RenderedPrompt:
    """Assemble the system + user prompt for one reasoning step.

    Order is deliberate and must not change: property facts, then knowledge,
    then the prompt's own System section (all stable across items), then the
    task instructions, then the item itself (volatile, last).
    """
    template = load_prompt(task)
    names = knowledge if knowledge is not None else template.meta.get("knowledge")

    system_parts = []
    if settings is not None:
        system_parts.append(hotel_block(settings))
    kb = load_knowledge(names)
    if kb:
        system_parts.append("### Property knowledge\n" + kb)
    if template.system:
        system_parts.append(render_string(template.system, _vars(settings, vars)))
    system = "\n\n".join(p for p in system_parts if p).strip()

    user_parts = []
    if template.body:
        user_parts.append(render_string(template.body, _vars(settings, vars)))
    if item is not None:
        payload = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False,
                                                                indent=2, default=str)
        user_parts.append("## Item\n```json\n" + payload + "\n```")
    return RenderedPrompt(
        system=system, user="\n\n".join(user_parts).strip(),
        fixture_id=fixture_id or template.meta.get("fixture_id"), meta=template.meta)


def _vars(settings: Settings | None, extra: dict) -> dict:
    base: dict[str, Any] = {}
    if settings is not None:
        base.update({
            "hotel_name": settings.hotel.name,
            "hotel_timezone": settings.hotel.timezone,
            "hotel_currency": settings.hotel.currency,
            "hotel_languages": ", ".join(settings.hotel.languages),
            "default_language": settings.hotel.default_language,
            "mode": settings.mode,
        })
    base.update(extra)
    return base
