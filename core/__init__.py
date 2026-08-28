"""core — the shared runtime vendored into every TH1 hotel agent template.

Every repo in the family carries a byte-identical copy of this package. It gives
the agent one config loader, one LLM entry point, one SQLite store with a review
state machine, one write guard, and one set of system adapters.

Nothing in here knows about a specific agent. Agent-specific logic lives in
``tools/`` and ``prompts/`` at the repo root.

Typical use from a tool::

    from core.config import load_settings
    from core.llm import complete
    from core.store import Store

    settings = load_settings()
    store = Store(settings)
    result = complete("classify", prompt, schema, settings=settings, store=store)
"""

CORE_VERSION = "1.0.0"

__all__ = ["CORE_VERSION"]
