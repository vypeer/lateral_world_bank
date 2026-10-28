"""Shared fixtures. Tests never touch the network: API responses come from tests/fixtures/."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
import requests

from wbpipe.config import Settings, parse_settings

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
LIVE = FIXTURES / "live"
BASE_URL = "https://api.worldbank.org/v2"

RouteKey = tuple[str, frozenset[tuple[str, str]], int]


def route_key(url: str) -> RouteKey:
    """Identify a request by path, params (minus paging) and page number."""
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query))
    page = int(params.pop("page", "1"))
    params.pop("per_page", None)
    return parts.path, frozenset(params.items()), page


@dataclass
class FakeResponse:
    status_code: int
    content: bytes
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeSession:
    """Serves captured fixtures by URL; tests can queue failures or override bodies."""

    routes: dict[RouteKey, bytes]
    calls: list[str] = field(default_factory=list)
    # url substring -> queued responses/exceptions served (in order) before the real fixture
    queued: dict[str, list[FakeResponse | Exception]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def get(self, url: str, timeout: float) -> FakeResponse:
        self.calls.append(url)
        for fragment, queue in self.queued.items():
            if fragment in url and queue:
                item = queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
        key = route_key(url)
        if key not in self.routes:
            return FakeResponse(404, b"no fixture for " + url.encode())
        return FakeResponse(200, self.routes[key])

    def override(self, fixture_url: str, body: bytes) -> None:
        self.routes[route_key(fixture_url)] = body


def load_manifest() -> list[dict[str, Any]]:
    return json.loads((LIVE / "manifest.json").read_text())


@pytest.fixture
def live_manifest() -> list[dict[str, Any]]:
    """Metadata (URL, status, sha256) for every captured live API response."""
    return load_manifest()


@pytest.fixture
def load_live():
    """Return a loader for a captured response by fixture name."""

    def _load(name: str) -> Any:
        return json.loads((LIVE / f"{name}.json").read_text())

    return _load


@pytest.fixture
def fake_session() -> FakeSession:
    routes = {
        route_key(e["url"]): (LIVE / f"{e['name']}.json").read_bytes()
        for e in load_manifest()
        if not e["name"].startswith("error_")
    }
    return FakeSession(routes=routes)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Repo config with paging and storage pointed at the fixtures and a temp dir."""
    return parse_settings(
        {
            "api": {"base_url": BASE_URL, "per_page": 50, "max_retries": 2, "backoff_seconds": 0},
            "years": {"start": 2018, "end": 2024},
            "countries": ["BRA", "CAN", "DEU", "GBR", "IND", "JPN", "MEX", "USA", "ZAF"],
            "indicators": [
                {"code": "SP.POP.TOTL", "short_name": "population"},
                {"code": "NY.GDP.MKTP.CD", "short_name": "gdp_usd"},
                {"code": "NY.GDP.PCAP.CD", "short_name": "gdp_per_capita_usd"},
                {"code": "SP.DYN.LE00.IN", "short_name": "life_expectancy_years"},
            ],
            "paths": {
                "landing_dir": str(tmp_path / "landing"),
                "warehouse": str(tmp_path / "warehouse.duckdb"),
                "sql_dir": str(REPO / "sql"),
                "output_dir": str(tmp_path / "output"),
            },
        },
        root=tmp_path,
    )


__all__ = ["FakeResponse", "FakeSession", "requests"]
