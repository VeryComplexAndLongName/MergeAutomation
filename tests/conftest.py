"""Shared pytest fixtures for gitea automation tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


@pytest.fixture()
def project_root() -> Path:
    """Return project root path for tests."""
    return Path(__file__).resolve().parents[1]
