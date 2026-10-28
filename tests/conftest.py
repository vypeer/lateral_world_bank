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
import yaml

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


COUNTRIES = "BRA;CAN;DEU;GBR;IND;JPN;MEX;USA;ZAF"


def edit_observations(fake_session: FakeSession, indicator: str, edit: Any, page: int = 2) -> None:
    """Serve a modified observation page; ``edit(meta, rows)`` mutates it in place.

    A change to meta.total is mirrored onto the other page, as the real API would report it.
    """
    url = f"{BASE_URL}/country/{COUNTRIES}/indicator/{indicator}?format=json&date=2018:2024"
    # Start from what is currently served, so several edits to the same page accumulate.
    pages = {n: json.loads(fake_session.routes[route_key(f"{url}&page={n}")]) for n in (1, 2)}
    meta, rows = pages[page]
    edit(meta, rows)
    fake_session.override(f"{url}&page={page}", json.dumps([meta, rows]).encode())
    other_meta, other_rows = pages[3 - page]
    if other_meta["total"] != meta["total"]:
        other_meta["total"] = meta["total"]
        fake_session.override(
            f"{url}&page={3 - page}", json.dumps([other_meta, other_rows]).encode()
        )


@pytest.fixture
def ingest(settings, fake_session):
    """Ingest from the fixtures under a given run id."""
    from wbpipe.api_client import WorldBankClient
    from wbpipe.ingest import run_ingest

    def _run(run_id: str):
        client = WorldBankClient(settings.api, session=fake_session, sleep=lambda _: None)
        return run_ingest(settings, client=client, run_id=run_id)

    return _run


@pytest.fixture
def ingest_with(fake_session):
    """Ingest from the fixtures with explicitly given settings."""
    from wbpipe.api_client import WorldBankClient
    from wbpipe.ingest import run_ingest

    def _run(settings: Settings, run_id: str):
        client = WorldBankClient(settings.api, session=fake_session, sleep=lambda _: None)
        return run_ingest(settings, client=client, run_id=run_id)

    return _run


@pytest.fixture
def pipeline(settings, ingest):
    """Ingest then transform, as `wbpipe run` would."""
    from wbpipe.transform import run_transform

    def _run(run_id: str):
        ingest(run_id)
        return run_transform(settings)

    return _run


def query(settings: Settings, sql: str, params: Any = None) -> list[Any]:
    import duckdb

    with duckdb.connect(str(settings.paths.warehouse), read_only=True) as con:
        return con.execute(sql, params or []).fetchall()


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
    """The repo config, with paging matching the fixtures and storage in a temp dir."""
    raw = yaml.safe_load((REPO / "config" / "pipeline.yaml").read_text())
    raw["api"].update(base_url=BASE_URL, per_page=50, max_retries=2, backoff_seconds=0)
    # The captured fixtures name these nine countries explicitly; 'all' mode has its own tests.
    raw["countries"] = COUNTRIES.split(";")
    raw["paths"] = {
        "landing_dir": str(tmp_path / "landing"),
        "warehouse": str(tmp_path / "warehouse.duckdb"),
        "sql_dir": str(REPO / "sql"),
        "output_dir": str(tmp_path / "output"),
    }
    return parse_settings(raw, root=REPO)


__all__ = ["FakeResponse", "FakeSession", "requests"]
