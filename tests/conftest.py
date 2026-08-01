"""Shared fixtures for LLPM tests."""

import shutil
from pathlib import Path

import pytest

from llpm import commands

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "docs"


@pytest.fixture
def docs_root(tmp_path):
    """Copy fixture data to a temp directory for isolated testing."""
    dst = tmp_path / "docs"
    shutil.copytree(FIXTURES_DIR, dst)
    return dst


@pytest.fixture(autouse=True)
def _hermetic_provenance(monkeypatch):
    """Keep the suite hermetic: pytest runs inside the llpm repo, so real
    commit harvesting would leak actual repo SHAs into fixture tickets, and
    ambient LLPM_* env would flip provenance defaults. Provenance tests
    re-patch what they need."""
    monkeypatch.delenv("LLPM_ORIGIN", raising=False)
    monkeypatch.delenv("LLPM_CREATED_BY", raising=False)
    monkeypatch.setattr(commands, "_harvest_commits", lambda ticket_id: [])
