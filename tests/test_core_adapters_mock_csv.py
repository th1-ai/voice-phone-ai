"""Tests for the universal CSV adapters — core.adapters.pms_csv / sheets_csv.

These are the "always works, no credentials" integrations every hotel starts
with (docs/integrations.md). This file also doubles as the template new
adapters should copy, per docs/integrations.md's "Write a test" rule.
"""

from __future__ import annotations

from core.adapters.base import AdapterConfig
from core.adapters.pms_csv import CsvPMS
from core.adapters.sheets_csv import CsvSheets
from core.config import HotelConfig, Settings
from core.review import WriteBlocked
from core.store import Store


def _write_csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(header)] + [",".join(row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _pms(tmp_path, monkeypatch, mode="shadow") -> CsvPMS:
    # AGENT_REPO_ROOT keeps write_log (data/exports/pms_writes.csv) inside
    # tmp_path too, even though `imports_dir` already redirects the reads.
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    settings = Settings(hotel=HotelConfig(currency="EUR"), mode=mode)
    config = AdapterConfig(adapter="csv", options={"imports_dir": str(tmp_path / "imports")})
    return CsvPMS(settings, config)


def test_ping_reports_no_exports_until_a_csv_exists(tmp_path, monkeypatch):
    pms = _pms(tmp_path, monkeypatch)
    health = pms.ping()
    assert health.ok is False

    _write_csv(tmp_path / "imports" / "reservations.csv",
              ["id", "check_in", "check_out"], [["r1", "2026-09-10", "2026-09-12"]])
    assert pms.ping().ok is True


def test_header_matching_is_loose_and_extra_columns_survive(tmp_path, monkeypatch):
    _write_csv(tmp_path / "imports" / "reservations.csv",
              ["ID", "CheckIn", "Check Out", "guestEmail", "Total", "Status"],
              [["r1", "2026-09-10", "2026-09-12", "guest@example.com", "450", "confirmed"]])
    pms = _pms(tmp_path, monkeypatch)
    reservations = pms.list_reservations("2026-09-01", "2026-09-30")
    assert len(reservations) == 1
    res = reservations[0]
    assert res.id == "r1"
    assert res.check_in == "2026-09-10"
    assert res.guest.email == "guest@example.com"
    assert res.total == 450.0
    assert res.extra["Status"] == "confirmed"


def test_list_reservations_filters_by_date_range(tmp_path, monkeypatch):
    _write_csv(tmp_path / "imports" / "reservations.csv",
              ["id", "check_in", "check_out"],
              [["in-range", "2026-09-10", "2026-09-12"],
               ["too-late", "2026-12-01", "2026-12-03"]])
    pms = _pms(tmp_path, monkeypatch)
    found = pms.list_reservations("2026-09-01", "2026-09-30")
    assert [r.id for r in found] == ["in-range"]


def test_get_reservation_matches_id_or_external_ref(tmp_path, monkeypatch):
    _write_csv(tmp_path / "imports" / "reservations.csv",
              ["id", "external_ref", "check_in", "check_out"],
              [["r1", "CONF-99", "2026-09-10", "2026-09-12"]])
    pms = _pms(tmp_path, monkeypatch)
    assert pms.get_reservation("r1").id == "r1"
    assert pms.get_reservation("CONF-99").id == "r1"
    assert pms.get_reservation("nope") is None


def test_find_guest_by_email_and_name(tmp_path, monkeypatch):
    _write_csv(tmp_path / "imports" / "guests.csv",
              ["id", "first_name", "last_name", "email"],
              [["g1", "Jamie", "Rivera", "jamie@example.com"]])
    pms = _pms(tmp_path, monkeypatch)
    by_email = pms.find_guest(email="jamie@example.com")
    by_name = pms.find_guest(name="rivera")
    assert [g.id for g in by_email] == ["g1"]
    assert [g.id for g in by_name] == ["g1"]


def test_rates_and_availability_from_csv(tmp_path, monkeypatch):
    _write_csv(tmp_path / "imports" / "rates.csv",
              ["date", "room_type_id", "price", "available", "closed"],
              [["2026-09-10", "double", "120", "3", ""],
               ["2026-09-10", "suite", "0", "0", "true"]])
    pms = _pms(tmp_path, monkeypatch)
    rates = pms.get_rates("2026-09-01", "2026-09-30")
    assert len(rates) == 2
    available = pms.get_availability("2026-09-01", "2026-09-30")
    assert [r.room_type_id for r in available] == ["double"]


def test_write_is_blocked_in_shadow_and_logged_when_allowed(tmp_path, monkeypatch):
    pms_shadow = _pms(tmp_path, monkeypatch, mode="shadow")
    try:
        pms_shadow.set_rate("2026-09-10", "double", 130.0)
    except WriteBlocked:
        pass
    else:
        raise AssertionError("expected WriteBlocked in shadow mode")

    # "pms_write" needs approval even in live mode (it is in the default
    # review.require_approval_for list) — pass an approved item to prove that
    # an item a human approved is what actually lets the write through.
    pms_live = _pms(tmp_path, monkeypatch, mode="live")
    store = Store(path=tmp_path / "guard.db")
    item = store.upsert_item("pms", "r1")
    store.transition(item.id, "pending_review")
    approved = store.transition(item.id, "approved")
    result = pms_live.add_note("r1", "guest requested a late checkout", item=approved)
    assert result["ok"] is True
    assert result["applied"] is False  # CSV mode never writes back to the PMS
    store.close()


def test_sheets_csv_write_read_append_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    settings = Settings(mode="live")
    config = AdapterConfig(adapter="csv", options={"exports_dir": str(tmp_path / "exports")})
    sheets = CsvSheets(settings, config)

    assert sheets.ping().ok is True
    sheets.write("upgrades", [["date", "room"], ["2026-09-10", "301"]])
    sheets.append("upgrades", [["2026-09-11", "302"]])
    rows = sheets.read("upgrades")
    assert rows == [["date", "room"], ["2026-09-10", "301"], ["2026-09-11", "302"]]


def test_signature_strips_yaml_frontmatter(tmp_path, monkeypatch):
    from core.adapters import get_email
    from core.config import load_settings
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    k = tmp_path / "knowledge"; k.mkdir(parents=True, exist_ok=True)
    (k / "signature.md").write_text('---\nsubject: ""\n---\nWarm regards,\nThe Team\n',
                                    encoding="utf-8")
    email = get_email(load_settings(provider="mock", mode="shadow"))
    sig = email.signature()
    assert sig.startswith("Warm regards"), sig
    assert "---" not in sig and "subject" not in sig


def test_csv_reservation_keeps_guest_level_columns(tmp_path, monkeypatch):
    """Tier / stay count / privacy notes on a reservations export reach the Guest."""
    from core.adapters.pms_csv import CsvPMS
    from core.config import load_settings
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    imports = tmp_path / "data" / "imports"; imports.mkdir(parents=True)
    (imports / "reservations.csv").write_text(
        "id,guest_id,guest_first_name,guest_last_name,guest_email,check_in,check_out,"
        "room_type_name,guest_vip,guest_language,guest_notes,tier,stays\n"
        "R1,G9,Ada,Lovelace,ada@example.com,2026-09-01,2026-09-04,Suite,true,it,"
        "\"discreet, no publicity please\",Platinum,9\n", encoding="utf-8")
    from core.config import AdapterConfig
    pms = CsvPMS(load_settings(provider="mock", mode="shadow"),
                 AdapterConfig(adapter="csv", options={}))
    res = pms.list_reservations("2026-08-01", "2026-09-30")
    assert len(res) == 1
    g = res[0].guest
    assert g.vip is True and g.language == "it"
    assert "no publicity" in g.notes
    assert g.extra.get("tier") == "Platinum" and g.extra.get("stays") == "9"


def test_messaging_appends_disclosure_line_once(tmp_path, monkeypatch):
    from core.adapters import get_messaging
    from core.config import load_settings
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    k = tmp_path / "knowledge"; k.mkdir(parents=True, exist_ok=True)
    (k / "disclosure.md").write_text("<!-- x -->\nDrafted with AI assistance.\n", encoding="utf-8")
    m = get_messaging(load_settings(provider="mock", mode="live"))
    out = m.with_disclosure("Your table is booked for 8pm.")
    assert out.endswith("Drafted with AI assistance.") and out.count("Drafted with AI") == 1
    assert m.with_disclosure(out) == out


def test_staff_chat_gets_no_guest_disclosure(tmp_path, monkeypatch):
    from core.adapters import get_messaging
    from core.config import load_settings
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    k = tmp_path / "knowledge"; k.mkdir(parents=True, exist_ok=True)
    (k / "disclosure.md").write_text("Drafted with AI assistance.", encoding="utf-8")
    m = get_messaging(load_settings(provider="mock", mode="live"))
    assert m.with_disclosure("Shift swap approved.", guest_facing=False) == "Shift swap approved."
    assert m.with_disclosure("Your table is booked.").endswith("Drafted with AI assistance.")
