"""Fetch API pages, land them verbatim, and register them in the raw layer.

A run is all-or-nothing for the warehouse: every page is fetched and landed first, then all of
them are registered in one transaction. A failed run is recorded in ``raw.ingest_run`` but adds
nothing to ``raw.api_fetch``, so downstream layers never see a partial snapshot.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from wbpipe.api_client import ApiPage, WorldBankApiError, WorldBankClient
from wbpipe.config import Settings
from wbpipe.warehouse import connect

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Request:
    """One logical API request (possibly many pages)."""

    endpoint: str  # country | indicator | observation
    entity: str | None
    path: str
    params: dict[str, str]

    @property
    def landing_subdir(self) -> Path:
        return Path(self.endpoint, self.entity) if self.entity else Path(self.endpoint)


@dataclass(frozen=True)
class IngestResult:
    run_id: str
    n_pages: int
    n_new_responses: int


# The API lists regional and income-group aggregates ("World", "High income", ...) alongside
# economies; they are marked with region id "NA".
AGGREGATE_REGION_ID = "NA"


def country_request(settings: Settings) -> Request:
    """Country metadata: the configured list, or every entry the API has in 'all' mode."""
    if settings.countries is None:
        return Request("country", None, "country", {})
    return Request("country", None, f"country/{';'.join(settings.countries)}", {})


def economies(pages: Sequence[ApiPage]) -> list[str]:
    """ISO3 codes of the economies (not aggregates) in country-metadata pages."""
    return [
        row["id"]
        for page in pages
        for row in page.rows
        if (row.get("region") or {}).get("id") != AGGREGATE_REGION_ID
    ]


def plan_requests(settings: Settings, countries: Sequence[str]) -> list[Request]:
    """Indicator metadata and observation requests for the resolved countries."""
    codes = ";".join(countries)
    reqs = []
    for ind in settings.indicators:
        reqs.append(Request("indicator", ind.code, f"indicator/{ind.code}", {}))
        reqs.append(
            Request(
                "observation",
                ind.code,
                f"country/{codes}/indicator/{ind.code}",
                {"date": f"{settings.start_year}:{settings.end_year}"},
            )
        )
    return reqs


def new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def response_id(request_url: str, payload_sha256: str) -> str:
    return hashlib.sha256(f"{request_url}\n{payload_sha256}".encode()).hexdigest()[:32]


def run_ingest(
    settings: Settings,
    client: WorldBankClient | None = None,
    run_id: str | None = None,
) -> IngestResult:
    client = client or WorldBankClient(settings.api)
    run_id = run_id or new_run_id()
    started_at = datetime.now(timezone.utc)
    run_dir = settings.paths.landing_dir / run_id
    log.info("ingest run %s started (landing to %s)", run_id, run_dir)

    # Fetch before opening the warehouse so its write lock is held only while registering.
    landed: list[tuple[Request, ApiPage, Path]] = []

    def fetch(req: Request) -> list[ApiPage]:
        pages = []
        for page in client.iter_pages(req.path, req.params):
            rel_path = Path(run_id) / req.landing_subdir / f"page_{page.page:04d}.json"
            target = settings.paths.landing_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(page.body)
            landed.append((req, page, rel_path))
            pages.append(page)
        log.info("fetched %s (%d page(s))", req.path[:80], len(pages))
        return pages

    try:
        # Countries first: in 'all' mode the observation requests depend on the economies listed.
        country_pages = fetch(country_request(settings))
        countries = settings.countries or tuple(economies(country_pages))
        if not countries:
            raise WorldBankApiError("the country endpoint listed no economies")
        log.info("%d countries in scope", len(countries))
        for req in plan_requests(settings, countries):
            fetch(req)
    except Exception as exc:
        with connect(settings) as con:
            _record_failure(con, settings, run_id, started_at, exc)
        raise

    with connect(settings) as con:
        try:
            n_new = _register(con, settings, run_id, started_at, landed)
        except Exception as exc:
            _record_failure(con, settings, run_id, started_at, exc)
            raise

    log.info("ingest run %s succeeded: %d pages, %d new responses", run_id, len(landed), n_new)
    return IngestResult(run_id=run_id, n_pages=len(landed), n_new_responses=n_new)


def _register(
    con: duckdb.DuckDBPyConnection,
    settings: Settings,
    run_id: str,
    started_at: datetime,
    landed: list[tuple[Request, ApiPage, Path]],
) -> int:
    con.execute("BEGIN TRANSACTION")
    try:
        before = _count_responses(con)
        for req, page, rel_path in landed:
            sha = hashlib.sha256(page.body).hexdigest()
            rid = response_id(page.request_url, sha)
            con.execute(
                """
                INSERT INTO raw.api_response VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, TRY_CAST(? AS DATE), ?, ?, ?, ?, ?)
                ON CONFLICT (response_id) DO NOTHING
                """,
                [
                    rid, req.endpoint, req.entity, page.request_url, json.dumps(page.params),
                    page.page, int(page.meta["pages"]), int(page.meta["total"]), page.http_status,
                    page.lastupdated, sha, page.body.decode("utf-8"), run_id, page.fetched_at,
                    rel_path.as_posix(),
                ],
            )  # fmt: skip
            con.execute(
                "INSERT INTO raw.api_fetch VALUES (?, ?, ?, ?)",
                [run_id, rid, page.fetched_at, rel_path.as_posix()],
            )
        n_new = _count_responses(con) - before
        con.execute(
            "INSERT INTO raw.ingest_run VALUES (?, ?, ?, 'succeeded', ?, ?, ?, NULL)",
            [run_id, started_at, datetime.now(timezone.utc), settings.fingerprint(),
             len(landed), n_new],
        )  # fmt: skip
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return n_new


def _record_failure(
    con: duckdb.DuckDBPyConnection,
    settings: Settings,
    run_id: str,
    started_at: datetime,
    exc: Exception,
) -> None:
    con.execute(
        """
        INSERT INTO raw.ingest_run VALUES (?, ?, ?, 'failed', ?, NULL, NULL, ?)
        ON CONFLICT (run_id) DO NOTHING
        """,
        [run_id, started_at, datetime.now(timezone.utc), settings.fingerprint(),
         f"{type(exc).__name__}: {exc}"],
    )  # fmt: skip


def _count_responses(con: duckdb.DuckDBPyConnection) -> int:
    row = con.execute("SELECT count(*) FROM raw.api_response").fetchone()
    return int(row[0]) if row else 0
