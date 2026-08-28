"""core.log — structured logging to ``data/logs/*.jsonl`` plus readable stdout.

Every tool opens one logger and every line carries the run id, so a week later
you can answer "what did the agent do on Tuesday at 09:15 and why".

    from core.log import get_logger, Run

    log = get_logger("triage")
    with Run("triage", settings, store) as run:
        log.info("fetched", count=12, run_id=run.id)
        run.stats["fetched"] = 12

Files rotate by day: ``data/logs/2026-08-27.jsonl``. Secrets never reach a log
line — values are truncated and any key that looks like a credential is masked.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import Settings, sub_data_dir

LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
_SECRET_HINTS = ("key", "token", "secret", "password", "passwd", "authorization", "cookie")


def _mask(key: str, value: Any) -> Any:
    if any(hint in key.lower() for hint in _SECRET_HINTS):
        return "***"
    if isinstance(value, str) and len(value) > 600:
        return value[:600] + "…"
    return value


class Logger:
    """Writes one JSON object per line, and a short human line to stdout."""

    def __init__(self, name: str, *, level: str = "info", quiet: bool = False) -> None:
        self.name = name
        self.level = LEVELS.get(level, 20)
        self.quiet = quiet

    @property
    def path(self) -> Path:
        """Resolved on every write, never cached: loggers are created at import
        time (module level) and must still follow a later ``AGENT_REPO_ROOT``
        (test sandboxes) and the day rolling over under a long ``make watch``."""
        return sub_data_dir("logs") / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"

    def _emit(self, level: str, message: str, fields: dict) -> None:
        if LEVELS.get(level, 20) < self.level:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": level, "logger": self.name, "message": message,
            **{k: _mask(k, v) for k, v in fields.items()},
        }
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass  # logging must never break the run
        if not self.quiet:
            extra = " ".join(f"{k}={v}" for k, v in fields.items()
                             if k not in ("run_id",) and v is not None)
            stream = sys.stderr if level in ("warn", "error") else sys.stdout
            print(f"[{level:<5}] {message}{(' ' + extra) if extra else ''}", file=stream)

    def debug(self, message: str, **fields: Any) -> None:
        self._emit("debug", message, fields)

    def info(self, message: str, **fields: Any) -> None:
        self._emit("info", message, fields)

    def warn(self, message: str, **fields: Any) -> None:
        self._emit("warn", message, fields)

    def error(self, message: str, **fields: Any) -> None:
        self._emit("error", message, fields)


def get_logger(name: str, *, level: str = "info", quiet: bool = False) -> Logger:
    """Return a logger writing to today's JSONL file."""
    return Logger(name, level=level, quiet=quiet)


@dataclass
class Run:
    """Context manager that opens a ``runs`` row and closes it with stats.

    ``run.stats`` is a plain dict you fill as you go; it is written to the row
    on exit, and ``tools/report.py`` reads it back.
    """

    workflow: str
    settings: Settings | None = None
    store: Any = None
    id: str = ""
    stats: dict = field(default_factory=dict)

    def __enter__(self) -> "Run":
        if self.store is not None:
            self.id = self.store.start_run(
                self.workflow,
                provider=self.settings.llm.provider if self.settings else "",
                mode=self.settings.mode if self.settings else "")
        if self.settings is not None:
            try:
                from core.adapters import sample_data_warning
                warning = sample_data_warning(self.settings)
            except Exception:  # never let a warning break a run
                warning = None
            if warning:
                self.stats["sample_data"] = True
                print(f"warning: {warning}", file=sys.stderr)
                get_logger("run", quiet=True).warn("sample_data", detail=warning)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc is not None:
            self.stats["error"] = f"{exc_type.__name__}: {exc}"[:500]
        if self.store is not None and self.id:
            self.store.finish_run(self.id, self.stats)


def summary_line(counts: dict[str, int], mode: str) -> str:
    """The one line every run prints last, e.g.

    ``DEMO OK — 3 items processed, 3 drafted, 0 sent (shadow)``
    """
    return (f"{counts.get('processed', 0)} items processed, "
            f"{counts.get('drafted', 0)} drafted, "
            f"{counts.get('sent', 0)} sent ({mode})")
