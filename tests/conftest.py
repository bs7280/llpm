"""Shared fixtures for LLPM tests."""

import shutil
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "docs"


@pytest.fixture
def docs_root(tmp_path):
    """Copy fixture data to a temp directory for isolated testing."""
    dst = tmp_path / "docs"
    shutil.copytree(FIXTURES_DIR, dst)
    return dst
