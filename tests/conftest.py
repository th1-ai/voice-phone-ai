"""Shared pytest fixtures.

`test_voicephone_*.py` must never read this repo's own `config/hotel.yaml` /
`config/agent.yaml` - those are the hotel's own edits, and a real property
filling them in (their own room types, their own currency, their own mode)
must never be able to turn `make test` red. See factory/workflows/
build-repo.md section 5 ("Tests never read the live config").

`test_core_*.py` (synced byte-identical from factory/core/ - never edit them
here) already manage their own `AGENT_REPO_ROOT` / `AGENT_CONFIG_DIR` per
test, so this fixture only isolates the voice-phone-ai-specific test files -
autouse, but a no-op for anything else.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated_config(request, tmp_path, monkeypatch):
    module_file = os.path.basename(getattr(request.module, "__file__", ""))
    if not module_file.startswith("test_voicephone_"):
        yield
        return

    cfg_dir = tmp_path / "isolated-config"
    cfg_dir.mkdir()
    shutil.copy(REPO_ROOT / "config" / "hotel.example.yaml", cfg_dir / "hotel.yaml")
    shutil.copy(REPO_ROOT / "config" / "agent.example.yaml", cfg_dir / "agent.yaml")
    monkeypatch.setenv("AGENT_CONFIG_DIR", str(cfg_dir))
    # Sandbox the repo root too: mock adapters write data/exports/*.jsonl under
    # repo_root(), and a test send must never leave a phantom "sent" record in
    # the hotel's own working copy.
    sandbox = tmp_path / "isolated-repo"
    if not sandbox.exists():
        sandbox.mkdir()
        for name in ("prompts", "fixtures"):
            src = REPO_ROOT / name
            if src.exists():
                shutil.copytree(src, sandbox / name)
        # knowledge/ and config/: the SHIPPED state right after `make setup`
        # (examples materialised), never the hotel's own edited files.
        for name in ("knowledge", "config"):
            src = REPO_ROOT / name
            dst = sandbox / name
            dst.mkdir(exist_ok=True)
            if src.exists():
                for f in src.iterdir():
                    if f.is_file() and (".example." in f.name or f.name == "README.md"):
                        shutil.copy(f, dst / f.name)
                        if ".example." in f.name:
                            real = dst / f.name.replace(".example.", ".")
                            if not real.exists():
                                shutil.copy(f, real)
    monkeypatch.setenv("AGENT_REPO_ROOT", str(sandbox))
    monkeypatch.delenv("AGENT_MODE", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_EFFORT", raising=False)
    yield
