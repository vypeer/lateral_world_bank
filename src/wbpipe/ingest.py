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
from typing import Any

import duckdb

from wbpipe.api_client import ApiPage, RetriesExhausted, WorldBankApiError, WorldBankClient
from wbpipe.config import Settings
from wbpipe.income_history import IncomeHistoryError, parse_oghist
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
    run_id: str  # when skipped: the earlier run whose data is still current
    n_pages: int
    n_new_responses: int
    skipped: bool = False


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


@dataclass(frozen=True)
class IncomeHistory:
    body: bytes  # the workbook, verbatim
    records: list[dict[str, Any]]


def download_income_history(settings: Settings, client: WorldBankClient) -> IncomeHistory | None:
    """The parsed OGHIST workbook, or None (with a warning) when disabled or unavailable.

    A failure here does not fail the run: income groups then fall back to the current
    classification and the income_history_loaded check warns.
    """
    if settings.income_history is None:
        return None
    try:
        body = client.fetch_file(settings.income_history.url)
        return IncomeHistory(body, parse_oghist(body, settings.income_history.sheet))
    except (WorldBankApiError, RetriesExhausted, IncomeHistoryError) as exc:
        log.warning("income history unavailable (%s); using current income groups", exc)
        return None


def income_history_page(settings: Settings, history: IncomeHistory) -> ApiPage:
    """The workbook's parsed rows as an API-shaped page, so raw lineage works unchanged."""
    assert settings.income_history is not None
    url, body, records = settings.income_history.url, history.body, history.records
    meta = {
        "page": 1, "pages": 1, "total": len(records), "lastupdated": None,
        "source_url": url, "sheet": settings.income_history.sheet,
        "file_sha256": hashlib.sha256(body).hexdigest(), "file_bytes": len(body),
    }  # fmt: skip
    payload = json.dumps([meta, records], sort_keys=True, separators=(",", ":")).encode()
    return ApiPage(
        path=url, params={}, request_url=url, page=1, http_status=200,
        fetched_at=datetime.now(timezone.utc), body=payload, meta=meta, rows=records,
    )  # fmt: skip


def unchanged_since(
    settings: Settings,
    client: WorldBankClient,
    country_pages: Sequence[ApiPage],
    history: IncomeHistory | None = None,
) -> str | None:
    """The last successful run if cheap probes show nothing changed since it, else None.

    Unchanged means: same scope fingerprint, byte-identical country metadata (which carries
    income groups and the list of economies), and for every indicator the same lastupdated
    date and row count (probed with per_page=1).
    """
    if not settings.paths.warehouse.exists():
        return None
    with connect(settings, read_only=True) as con:
        last = con.execute(
            "SELECT run_id, config_fingerprint FROM raw.ingest_run WHERE status = 'succeeded' "
            "ORDER BY finished_at DESC, run_id DESC LIMIT 1"
        ).fetchone()
        if last is None or last[1] != settings.fingerprint():
            return None
        seen = con.execute(
            """
            SELECT r.endpoint, r.entity, r.page, r.payload_sha256,
                   CAST(r.source_lastupdated AS VARCHAR), r.total_rows
            FROM raw.api_fetch f JOIN raw.api_response r USING (response_id)
            WHERE f.run_id = ? AND (r.endpoint = 'country' OR (r.endpoint = 'observation'
                                                                AND r.page = 1))
            ORDER BY r.endpoint, r.entity, r.page
            """,
            [last[0]],
        ).fetchall()
        history_sha = con.execute(
            """
            SELECT r.payload ->> '$[0].file_sha256'
            FROM raw.api_fetch f JOIN raw.api_response r USING (response_id)
            WHERE f.run_id = ? AND r.endpoint = 'income_history'
            """,
            [last[0]],
        ).fetchone()
    previous_history = history_sha[0] if history_sha else None
    current_history = hashlib.sha256(history.body).hexdigest() if history is not None else None
    if current_history is not None and current_history != previous_history:
        log.info("income history workbook changed since run %s", last[0])
        return None
    country_shas = [row[3] for row in seen if row[0] == "country"]
    if country_shas != [hashlib.sha256(p.body).hexdigest() for p in country_pages]:
        log.info("country metadata changed since run %s", last[0])
        return None
    previous = {row[1]: (row[4], row[5]) for row in seen if row[0] == "observation"}
    countries = settings.countries or tuple(economies(country_pages))
    for req in plan_requests(settings, countries):
        if req.endpoint != "observation":
            continue
        probe = client.first_page(req.path, req.params, per_page=1)
        current = (probe.lastupdated, int(probe.meta["total"]))
        if previous.get(req.entity) != current:
            log.info("%s changed since run %s: %s -> %s",
                     req.entity, last[0], previous.get(req.entity), current)  # fmt: skip
            return None
    return str(last[0])


def run_ingest(
    settings: Settings,
    client: WorldBankClient | None = None,
    run_id: str | None = None,
    force: bool = False,
) -> IngestResult:
    client = client or WorldBankClient(settings.api)
    country_req = country_request(settings)
    prefetched: list[ApiPage] | None = None
    history = download_income_history(settings, client)
    if settings.skip_if_unchanged and not force:
        prefetched = client.fetch_all(country_req.path, country_req.params)
        since = unchanged_since(settings, client, prefetched, history)
        if since:
            with connect(settings) as con:
                con.execute(
                    "INSERT INTO raw.ingest_skip VALUES (?, ?)",
                    [datetime.now(timezone.utc), since],
                )
            log.info("source unchanged since ingest run %s; skipping the fetch "
                     "(use --force to fetch anyway)", since)  # fmt: skip
            return IngestResult(run_id=since, n_pages=0, n_new_responses=0, skipped=True)

    run_id = run_id or new_run_id()
    started_at = datetime.now(timezone.utc)
    run_dir = settings.paths.landing_dir / run_id
    log.info("ingest run %s started (landing to %s)", run_id, run_dir)

    # Fetch before opening the warehouse so its write lock is held only while registering.
    landed: list[tuple[Request, ApiPage, Path]] = []

    def fetch(req: Request, already: list[ApiPage] | None = None) -> list[ApiPage]:
        pages = []
        for page in already if already is not None else client.iter_pages(req.path, req.params):
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
        country_pages = fetch(country_req, prefetched)
        countries = settings.countries or tuple(economies(country_pages))
        if not countries:
            raise WorldBankApiError("the country endpoint listed no economies")
        log.info("%d countries in scope", len(countries))
        for req in plan_requests(settings, countries):
            fetch(req)
        if history is not None:
            assert settings.income_history is not None
            req = Request("income_history", None, settings.income_history.url, {})
            target = settings.paths.landing_dir / run_id / req.landing_subdir
            target.mkdir(parents=True, exist_ok=True)
            # the workbook verbatim, next to the parsed page registered in raw
            (target / "OGHIST.xlsx").write_bytes(history.body)
            fetch(req, [income_history_page(settings, history)])
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
