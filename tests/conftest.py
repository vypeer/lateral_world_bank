"""Shared fixtures. Tests never touch the network: API responses come from tests/fixtures/."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
LIVE = FIXTURES / "live"


@pytest.fixture
def live_manifest() -> list[dict[str, Any]]:
    """Metadata (URL, status, sha256) for every captured live API response."""
    return json.loads((LIVE / "manifest.json").read_text())


@pytest.fixture
def load_live():
    """Return a loader for a captured response by fixture name."""

    def _load(name: str) -> Any:
        return json.loads((LIVE / f"{name}.json").read_text())

    return _load
