"""core.doctor — the health check behind ``make doctor``.

Answers one question: *would the agent work right now, and if not, what exactly
do I have to fix?* Every failing check carries a fix hint that names a file, a
variable or a command. No stack traces.

    from core.doctor import run_checks, print_table
    checks = run_checks(settings)
    raise SystemExit(print_table(checks))

``print_table`` returns the process exit code: 0 when nothing failed, 1 when
something did. Warnings never fail the run — they are things worth knowing that
do not stop the agent (shadow mode, an empty review queue, a stub adapter).
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core import CORE_VERSION
from core.config import Settings, config_path, repo_root

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_SYMBOL = {PASS: "ok", WARN: "warn", FAIL: "FAIL"}


@dataclass
class Check:
    """One line of the doctor table."""

    name: str
    status: str = PASS
    detail: str = ""
    fix_hint: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAIL


# --------------------------------------------------------------------------
# individual checks
# --------------------------------------------------------------------------
def check_python() -> Check:
    major, minor = sys.version_info[:2]
    version = f"{major}.{minor}.{sys.version_info[2]}"
    if (major, minor) < (3, 11):
        return Check("python", FAIL, f"{version} is too old",
                     "This agent needs Python 3.11 or newer. Install it and re-run "
                     "`make setup`.")
    return Check("python", PASS, f"{version} (core {CORE_VERSION})")


def check_dependencies() -> Check:
    missing = []
    try:
        import yaml  # noqa: F401
    except ImportError:
        missing.append("pyyaml")
    if missing:
        return Check("dependencies", FAIL, f"missing {', '.join(missing)}",
                     "Run `make setup` (it creates .venv and installs requirements.txt).")
    return Check("dependencies", PASS, "pyyaml present")


def check_config() -> list[Check]:
    out = []
    for name in ("hotel", "agent"):
        path = config_path(name)
        if path is None:
            out.append(Check(f"config/{name}.yaml", FAIL, "not found",
                             f"Run `make setup`, or copy config/{name}.example.yaml to "
                             f"config/{name}.yaml."))
        elif path.name.endswith(".example.yaml"):
            out.append(Check(f"config/{name}.yaml", WARN, "using the shipped example",
                             f"Copy config/{name}.example.yaml to config/{name}.yaml and "
                             f"put your own details in it."))
        else:
            out.append(Check(f"config/{name}.yaml", PASS, str(path.name)))
    return out


def check_env_file() -> Check:
    path = repo_root() / ".env"
    if not path.exists():
        return Check(".env", WARN, "not found",
                     "Run `make setup` to create it from .env.example. Fine to skip "
                     "while you are on mock adapters.")
    filled = [line for line in path.read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.strip().startswith("#") and
              line.partition("=")[2].strip()]
    if not filled:
        return Check(".env", WARN, "present but every value is blank",
                     "Fill in the variables for the adapters you enabled, then re-run "
                     "`make doctor`.")
    return Check(".env", PASS, f"{len(filled)} values set")


def check_hotel_identity(settings: Settings) -> Check:
    hotel = settings.hotel
    placeholders = {"Your Hotel", "Hotel Aurora", "", "The Marlow House"}
    if hotel.name in placeholders:
        # FAIL, not WARN: a placeholder name means the agent would quote the
        # wrong property to guests. A fresh clone (config/*.yaml copied verbatim
        # from the shipped example by `make setup`) must fail `make doctor`
        # until the hotel fills in real details — see ARCHITECTURE.md section 8
        # ("make doctor with an empty .env exits non-zero") and
        # factory/workflows/build-repo.md section 4 ("doctor exits 1 on empty
        # .env — expected"). Leaving this as WARN meant a totally unconfigured
        # repo silently reported all-clear.
        return Check("hotel identity", FAIL, f"name is still '{hotel.name}'",
                     "Put your real property details in config/hotel.yaml — the agent "
                     "quotes them to guests.")
    return Check("hotel identity", PASS,
                 f"{hotel.name}, {hotel.timezone}, {hotel.currency}, "
                 f"languages {'/'.join(hotel.languages)}")


def check_mode(settings: Settings) -> Check:
    if settings.mode == "shadow":
        return Check("mode", WARN, "shadow — the agent drafts but never sends",
                     "That is the right place to start. workflows/90-go-live.md walks "
                     "you through switching to live.")
    return Check("mode", PASS, "live — approved items will really be sent")


def check_llm(settings: Settings) -> Check:
    provider = settings.llm.provider
    if provider == "mock":
        return Check("llm provider", WARN, "mock — canned answers, no real reasoning",
                     "Fine for demo and tests. Set llm.provider to interactive, "
                     "claude-code or anthropic for real work.")
    if provider == "interactive":
        return Check("llm provider", PASS,
                     "interactive — prompts are parked in data/pending for your "
                     "Claude session to answer")
    if provider == "claude-code":
        if not shutil.which("claude"):
            return Check("llm provider", FAIL, "claude-code selected but `claude` is not "
                         "on PATH",
                         "Install Claude Code and run `claude` once to log in, or switch "
                         "llm.provider to anthropic or interactive.")
        return Check("llm provider", PASS, f"claude-code, model {settings.llm.model}")
    if provider == "anthropic":
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return Check("llm provider", FAIL, "anthropic selected but the SDK is missing",
                         "Run `pip install anthropic`, or switch llm.provider.")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return Check("llm provider", FAIL, "ANTHROPIC_API_KEY is not set",
                         "Add it to .env, or switch llm.provider to claude-code to use "
                         "your Claude Code login instead.")
        return Check("llm provider", PASS, f"anthropic, model {settings.llm.model}")
    return Check("llm provider", FAIL, f"unknown provider '{provider}'",
                 "Use mock, interactive, claude-code or anthropic.")


def check_adapters(settings: Settings) -> list[Check]:
    from core.adapters import get_all
    out = []
    for system, adapter in get_all(settings).items():
        health = adapter.ping()
        caps = ", ".join(sorted(adapter.capabilities())[:4]) or "none"
        status = PASS if health.ok else (WARN if adapter.status == "stub" else FAIL)
        detail = f"{adapter.name} [{adapter.status}] {health.detail}"
        if health.ok:
            detail += f" | can: {caps}"
        out.append(Check(f"{system} adapter", status, detail, health.fix_hint))
    return out


def check_store(settings: Settings) -> Check:
    try:
        from core.store import Store
        with Store(settings) as store:
            counts = store.counts()
    except Exception as exc:  # noqa: BLE001
        return Check("store", FAIL, f"cannot open {settings.db_path()}: {exc}"[:160],
                     "Delete data/agent.db to start clean, or check folder permissions.")
    if not counts:
        return Check("store", PASS, "database ready (no items yet)")
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    return Check("store", PASS, summary)


def check_knowledge() -> Check:
    base = repo_root() / "knowledge"
    # knowledge/README.md documents the folder itself, not property knowledge -
    # it must not count as "you have filled this in" (it ships in every repo).
    real = ([p for p in base.glob("*.md") if ".example." not in p.name
             and p.name != "README.md"] if base.exists() else [])
    if not real:
        return Check("knowledge", WARN, "only example files",
                     "Copy knowledge/property.example.md to knowledge/property.md and "
                     "fill in what this agent actually reads — knowledge/README.md in this "
                     "repo says which files matter here.")
    names = ", ".join(p.name for p in real[:4])
    if len(real) > 4:
        names += f" (+{len(real) - 4} more)"
    return Check("knowledge", PASS, f"{len(real)} file(s): {names}")


# --------------------------------------------------------------------------
# runner + printer
# --------------------------------------------------------------------------
def run_checks(settings: Settings | None = None,
               extra: list[Callable[[Settings], Any]] | None = None) -> list[Check]:
    """Run the generic checks, plus any agent-specific ones passed in ``extra``.

    An agent's ``tools/doctor.py`` adds its own checks like this::

        def check_intents(settings):
            return Check("intents", PASS, f"{len(settings.agent_get('intents', []))} intents")
        run_checks(settings, extra=[check_intents])
    """
    checks = [check_python(), check_dependencies()]
    checks += check_config()
    checks.append(check_env_file())
    if settings is None:
        return checks
    checks += [check_hotel_identity(settings), check_mode(settings), check_llm(settings)]
    checks += check_adapters(settings)
    checks += [check_store(settings), check_knowledge()]
    for func in extra or []:
        try:
            result = func(settings)
        except Exception as exc:  # noqa: BLE001 - a broken check must not hide the table
            result = Check(getattr(func, "__name__", "custom check"), FAIL,
                           f"check raised: {exc}"[:160], "")
        checks += result if isinstance(result, list) else [result]
    return checks


def print_table(checks: list[Check], *, title: str = "", stream: Any = None) -> int:
    """Print the doctor table. Returns the exit code (0 healthy, 1 something failed)."""
    stream = stream or sys.stdout
    width = max([len(c.name) for c in checks] + [6])
    if title:
        print(f"\n{title}", file=stream)
    print("-" * (width + 60), file=stream)
    for check in checks:
        print(f"  {_SYMBOL[check.status]:<5} {check.name:<{width}}  {check.detail}",
              file=stream)
        if check.fix_hint and check.status != PASS:
            print(f"        {' ' * width}  -> {check.fix_hint}", file=stream)
    print("-" * (width + 60), file=stream)
    failed = [c for c in checks if c.failed]
    warned = [c for c in checks if c.status == WARN]
    if failed:
        print(f"  {len(failed)} check(s) failed, {len(warned)} warning(s). "
              f"Fix the FAIL lines above and run `make doctor` again.", file=stream)
        return 1
    print(f"  All checks passed ({len(warned)} warning(s)).", file=stream)
    return 0
