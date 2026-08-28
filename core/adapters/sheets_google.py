"""Google Sheets adapter — status: built. Live shared sheets for the team.

Use this when the hotel already lives in Google Sheets and wants the agent's
output to appear in a sheet everyone can see, rather than a CSV on one machine.

**Setup.** Two options; a service account is better for a machine that runs
unattended.

*Service account (recommended for cron)*

1. Google Cloud Console: create a project, enable the **Google Sheets API**.
2. Create a **service account**, then a JSON key for it. Save it as
   ``service_account.json`` in this repo (gitignored).
3. Share your spreadsheet with the service account's email address
   (``something@project.iam.gserviceaccount.com``) as an Editor.
4. ``pip install google-api-python-client google-auth``

*Desktop OAuth (same flow as the Gmail adapter)*

Reuses ``credentials.json`` / ``token.json``. Add
``https://www.googleapis.com/auth/spreadsheets`` to the scopes.

``config/hotel.yaml``::

    systems:
      sheets:
        adapter: google
        spreadsheet_id: 1AbC...        # the long id in the sheet's URL
        service_account_file: service_account.json

Sheet names are tab names. A tab that does not exist is created on first write.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

from core.adapters.base import AdapterNotConfigured, HealthCheck, Sheets, guarded_write
from core.config import repo_root

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_INSTALL_HINT = ("pip install google-api-python-client google-auth "
                 "(or use systems.sheets.adapter: csv, which needs nothing)")


class GoogleSheets(Sheets):
    """Reads and writes a Google spreadsheet the hotel owns."""

    status, name = "built", "sheets_google"

    def __init__(self, settings: Any, config: Any = None) -> None:
        super().__init__(settings, config)
        self.spreadsheet_id = str(self.opt("spreadsheet_id", "", env="GOOGLE_SHEET_ID"))
        self.service_account_file = Path(self.opt(
            "service_account_file", repo_root() / "service_account.json",
            env="GOOGLE_SERVICE_ACCOUNT_FILE"))
        self.token_file = Path(self.opt("token_file", repo_root() / "token.json",
                                        env="GOOGLE_TOKEN_FILE"))
        self._service: Any = None

    # -- auth -------------------------------------------------------------
    def _client(self) -> Any:
        if self._service is not None:
            return self._service
        try:
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise AdapterNotConfigured(
                f"sheets_google: Google client libraries are not installed. {_INSTALL_HINT}"
            ) from exc

        creds = None
        if self.service_account_file.exists():
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_file(
                str(self.service_account_file), scopes=SCOPES)
        elif self.token_file.exists():
            from google.oauth2.credentials import Credentials
            creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
        if creds is None:
            raise AdapterNotConfigured(
                f"sheets_google: no credentials. Save a service account key at "
                f"{self.service_account_file} and share the sheet with its email address "
                "(see docs/integrations.md#sheets).")
        self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return self._service

    # -- introspection ----------------------------------------------------
    def ping(self) -> HealthCheck:
        if not self.spreadsheet_id:
            return HealthCheck(False, self.name, "systems.sheets.spreadsheet_id is not set",
                               "Copy the long id out of your sheet's URL into "
                               "config/hotel.yaml.")
        if not self.service_account_file.exists() and not self.token_file.exists():
            return HealthCheck(False, self.name, "no Google credentials found",
                               f"Save a service account key at {self.service_account_file}.")
        try:
            meta = self._client().spreadsheets().get(
                spreadsheetId=self.spreadsheet_id).execute()
        except Exception as exc:  # noqa: BLE001 - ping never raises
            return HealthCheck(False, self.name, str(exc)[:200],
                               "Share the spreadsheet with the service account email "
                               "as an Editor.")
        tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
        return HealthCheck(True, self.name,
                           f"'{meta.get('properties', {}).get('title', '?')}' "
                           f"({len(tabs)} tabs: {', '.join(tabs[:5])})")

    def capabilities(self) -> set[str]:
        return {"read", "append", "write"}

    # -- helpers ----------------------------------------------------------
    def _ensure_tab(self, sheet: str) -> None:
        service = self._client()
        meta = service.spreadsheets().get(spreadsheetId=self.spreadsheet_id).execute()
        titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
        if sheet in titles:
            return
        service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": sheet}}}]}).execute()

    # -- operations -------------------------------------------------------
    def read(self, sheet: str) -> list[list[Any]]:
        result = self._client().spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id, range=sheet).execute()
        return result.get("values", [])

    @guarded_write("sheets_write")
    def append(self, sheet: str, rows: Iterable[Iterable[Any]]) -> dict:
        payload = [[("" if c is None else str(c)) for c in row] for row in rows]
        self._ensure_tab(sheet)
        result = self._client().spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id, range=sheet,
            valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
            body={"values": payload}).execute()
        return {"ok": True, "rows": len(payload),
                "updated_range": (result.get("updates") or {}).get("updatedRange")}

    @guarded_write("sheets_write")
    def write(self, sheet: str, rows: Iterable[Iterable[Any]]) -> dict:
        """Replace the tab's contents. Clears first, so it is destructive."""
        payload = [[("" if c is None else str(c)) for c in row] for row in rows]
        self._ensure_tab(sheet)
        service = self._client()
        service.spreadsheets().values().clear(
            spreadsheetId=self.spreadsheet_id, range=sheet, body={}).execute()
        result = service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id, range=sheet,
            valueInputOption="USER_ENTERED", body={"values": payload}).execute()
        return {"ok": True, "rows": len(payload),
                "updated_range": result.get("updatedRange")}
