"""The `[SAMPLE DATA]` marker in `tools/review.py` (`list` and `show`).

A hotel that runs the real loop on a fresh clone is reading the shipped
fixtures, not its own calls: every `config/*.example.yaml` ships with
`systems.email.adapter: mock`. `core.store.Store.upsert_item` tags what such
a pass creates with payload `_sample: True` (`core.adapters.is_sample_source`,
narrowed by `systems_used` in `config/agent.yaml`) and `item.is_sample` reads
it back. This repo does not re-implement the tagging - it only has to SHOW
it, so nothing that came out of `fixtures/inbound/` can be approved as if a
real caller had left it.

Named `test_voicephone_*` on purpose: `tests/conftest.py`'s autouse fixture
isolates AGENT_CONFIG_DIR / AGENT_REPO_ROOT for that prefix only, so this
module never reads the property's own filled-in config.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.config import load_settings
from core.store import Store

import store_ext
from review import cmd_list, cmd_show


def _sample_call(tmp_path, name="sample.db"):
    """One queued call from a real (not `make demo`) pass on the shipped defaults."""
    settings = load_settings()
    assert settings.demo is False                    # the real path, not `make demo`
    assert settings.systems.email.adapter == "mock"  # the shipped default
    store = Store(settings, path=tmp_path / name)
    store_ext.ensure_schema(store)
    item = store.upsert_item("email", "sample-call-1", kind="call",
                             payload={"subject": "Voicemail 09:12",
                                      "transcript": "Hello, this is Marie - do you have a "
                                                    "sea view room for two nights in June?"})
    store.set_fields(item.id, intent="room_booking",
                     draft={"subject": "Your room enquiry", "body": "Yes, we do.",
                           "needs_human": False, "channel": "email",
                           "caller": {"name": "Marie", "email": "marie@example.com"},
                           "pending_booking": {"kind": "none"}})
    return store, store.transition(item.id, "pending_review", "agent")


def test_a_call_read_through_the_mock_mailbox_is_tagged_sample(tmp_path):
    store, item = _sample_call(tmp_path)
    store.close()
    assert item.is_sample is True
    assert item.payload.get("_sample") is True


def test_review_list_marks_the_sample_call(tmp_path, capsys):
    store, _ = _sample_call(tmp_path, "sample-list.db")
    capsys.readouterr()
    cmd_list(store, argparse.Namespace(status=None, kind=None, limit=50))
    store.close()
    out = capsys.readouterr().out
    assert "[SAMPLE DATA]" in out
    assert "shipped sample fixtures" in out


def test_review_show_warns_before_the_json(tmp_path, capsys):
    store, item = _sample_call(tmp_path, "sample-show.db")
    capsys.readouterr()
    cmd_show(store, argparse.Namespace(id=item.id))
    store.close()
    out = capsys.readouterr().out
    assert out.startswith("[SAMPLE DATA]")
