"""Universal reporting output: CSV files in ``data/exports``.

The agent's job is not finished when it decides something — a person has to be
able to see it. This adapter writes one CSV per "sheet" name, which opens in
Excel, Numbers or Google Sheets with a double click.

``append`` adds rows to ``data/exports/<sheet>.csv`` (creating it with a header
row if the first row you pass looks like headers). ``write`` replaces the file.
``read`` returns the rows as lists of strings.

No credentials, no setup, works everywhere. Switch to ``sheets_google`` when the
team wants a live shared sheet instead of a file on one machine.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable

from core.adapters.base import HealthCheck, Sheets, guarded_write
from core.config import sub_data_dir


class CsvSheets(Sheets):
    """CSV files in ``data/exports``. The always-works reporting target."""

    status, name = "universal", "sheets_csv"

    def __init__(self, settings: Any, config: Any = None) -> None:
        super().__init__(settings, config)
        configured = self.opt("exports_dir")
        self.dir = Path(configured) if configured else sub_data_dir("exports")

    def _path(self, sheet: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(sheet))
        return self.dir / f"{safe or 'sheet'}.csv"

    def ping(self) -> HealthCheck:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            probe = self.dir / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            return HealthCheck(False, self.name, f"cannot write to {self.dir}: {exc}",
                               "Check the folder permissions.")
        existing = len(list(self.dir.glob("*.csv")))
        return HealthCheck(True, self.name, f"{self.dir} writable ({existing} CSV files)")

    def capabilities(self) -> set[str]:
        return {"read", "append", "write"}

    def read(self, sheet: str) -> list[list[Any]]:
        path = self._path(sheet)
        if not path.exists():
            return []
        with path.open(newline="", encoding="utf-8-sig") as fh:
            return [row for row in csv.reader(fh)]

    @guarded_write("sheets_write")
    def append(self, sheet: str, rows: Iterable[Iterable[Any]]) -> dict:
        path = self._path(sheet)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [list(r) for r in rows]
        with path.open("a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
        return {"ok": True, "path": str(path), "rows": len(rows)}

    @guarded_write("sheets_write")
    def write(self, sheet: str, rows: Iterable[Iterable[Any]]) -> dict:
        path = self._path(sheet)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [list(r) for r in rows]
        with path.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
        return {"ok": True, "path": str(path), "rows": len(rows)}
