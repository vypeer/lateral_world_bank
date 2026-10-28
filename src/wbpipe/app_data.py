"""Read-only data access for the live dashboard app.

Every query opens a short-lived read-only connection and closes it straight away, so the app
never holds the warehouse lock: `wbpipe run` can rebuild the warehouse while the app is open.
All filter values are passed as bound parameters, never interpolated into SQL.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

MAX_SQL_ROWS = 5000


class WarehouseUnavailable(RuntimeError):
    """The warehouse does not exist yet, or is locked by a pipeline run in progress."""


@dataclass(frozen=True)
class Warehouse:
    path: Path

    def version(self) -> int:
        """Changes whenever the warehouse file is rewritten; used as a cache key."""
        try:
            return os.stat(self.path).st_mtime_ns
        except FileNotFoundError:
            return 0

    def query(self, sql: str, params: Sequence[Any] | None = None) -> pd.DataFrame:
        if not self.path.exists():
            raise WarehouseUnavailable(f"no warehouse at {self.path}; run `make run` to build it")
        try:
            with duckdb.connect(str(self.path), read_only=True) as con:
                return con.execute(sql, list(params or [])).df()
        except (duckdb.IOException, duckdb.ConnectionException) as exc:
            # IOException: another process holds the write lock; ConnectionException: a
            # read-write connection to the same file is open in this process.
            raise WarehouseUnavailable(
                "the warehouse is being updated by a pipeline run; try again in a moment"
            ) from exc
        except duckdb.CatalogException as exc:
            raise WarehouseUnavailable(
                f"the warehouse is incomplete ({exc}); run `make run` to rebuild it"
            ) from exc


# ---- reference data ----------------------------------------------------------------------


def status(wh: Warehouse) -> dict[str, Any]:
    """What the app is showing: build, source freshness and data-quality summary."""
    build = wh.query(
        """
        SELECT t.transformed_at, t.ingest_run_id, r.finished_at AS fetched_at,
               (SELECT max(source_lastupdated) FROM core.fct_indicator_value) AS source_lastupdated
        FROM core.transform_run t JOIN raw.ingest_run r ON r.run_id = t.ingest_run_id
        ORDER BY t.transformed_at DESC LIMIT 1
        """
    )
    dq = wh.query(
        """
        SELECT status, count(*) AS n FROM quality.dq_result
        WHERE checked_at = (SELECT max(checked_at) FROM quality.dq_result)
        GROUP BY status
        """
    )
    counts = dict(zip(dq["status"], dq["n"], strict=True)) if len(dq) else {}
    out: dict[str, Any] = {str(k): v for k, v in build.iloc[0].items()} if len(build) else {}
    out["dq"] = {s: int(counts.get(s, 0)) for s in ("pass", "warn", "fail", "error")}
    out["dq_checked"] = bool(counts)
    return out


def countries(wh: Warehouse) -> pd.DataFrame:
    return wh.query(
        """
        SELECT country_iso3, coalesce(country_name, country_iso3) AS country_name, region_name,
               income_group_id, income_group_name, income_group_rank
        FROM core.dim_country ORDER BY config_order
        """
    )


def indicators(wh: Warehouse) -> pd.DataFrame:
    return wh.query(
        """
        SELECT d.indicator_code, d.short_name, coalesce(d.indicator_name, d.short_name) AS name,
               d.source_note, c.aggregation
        FROM core.dim_indicator d JOIN config.indicator c USING (indicator_code)
        ORDER BY d.config_order
        """
    )


def years(wh: Warehouse) -> list[int]:
    return [int(y) for y in wh.query("SELECT year FROM core.dim_year ORDER BY year")["year"]]


# ---- analytical queries (all filtered by the app's controls) ----------------------------------


def values(
    wh: Warehouse, indicator: str, iso3: Sequence[str], year_from: int, year_to: int
) -> pd.DataFrame:
    return wh.query(
        """
        SELECT country_iso3, country_name, income_group_name, year, value, value_status,
               obs_status, response_id
        FROM marts.v_indicator_value
        WHERE indicator_code = ? AND list_contains(?, country_iso3) AND year BETWEEN ? AND ?
        ORDER BY country_order, year
        """,
        [indicator, list(iso3), year_from, year_to],
    )


def comparison(
    wh: Warehouse, indicator: str, iso3: Sequence[str], year: int | None
) -> tuple[int | None, pd.DataFrame]:
    """Values at ``year``, or (None) at the latest year every selected country has a value."""
    if year is None:
        found = wh.query(
            """
            SELECT max(year) AS y FROM (
                SELECT year FROM marts.v_indicator_value
                WHERE indicator_code = ? AND list_contains(?, country_iso3)
                GROUP BY year HAVING count(*) FILTER (WHERE has_value) = len(?)
            )
            """,
            [indicator, list(iso3), list(iso3)],
        )["y"]
        year = None if found.isna().all() else int(found.iloc[0])
    if year is None:
        return None, pd.DataFrame()
    df = wh.query(
        """
        SELECT country_iso3, country_name, income_group_name, value, value_status,
               rank() OVER (ORDER BY value DESC NULLS LAST) AS rank,
               100 * value / sum(value) OVER () AS pct_of_selected_total
        FROM marts.v_indicator_value
        WHERE indicator_code = ? AND list_contains(?, country_iso3) AND year = ?
        ORDER BY value DESC NULLS LAST
        """,
        [indicator, list(iso3), year],
    )
    return year, df


def yoy(
    wh: Warehouse, indicator: str, iso3: Sequence[str], year_from: int, year_to: int
) -> pd.DataFrame:
    return wh.query(
        """
        SELECT country_iso3, country_name, year, value, prior_value, yoy_status,
               yoy_abs_change, yoy_pct_change, last_available_year,
               annualized_pct_change_since_last_available
        FROM marts.v_yoy_change
        WHERE indicator_code = ? AND list_contains(?, country_iso3) AND year BETWEEN ? AND ?
        ORDER BY country_iso3, year
        """,
        [indicator, list(iso3), year_from, year_to],
    )


def income_groups(
    wh: Warehouse, indicator: str, groups: Sequence[str], year_from: int, year_to: int
) -> pd.DataFrame:
    return wh.query(
        """
        SELECT income_group_id, income_group_name, income_group_rank, year, aggregation_method,
               aggregate_value, simple_mean, countries_in_group, countries_in_aggregate,
               is_complete, countries
        FROM marts.v_income_group_by_year
        WHERE indicator_code = ? AND list_contains(?, income_group_id) AND year BETWEEN ? AND ?
        ORDER BY income_group_rank DESC, year
        """,
        [indicator, list(groups), year_from, year_to],
    )


def completeness(wh: Warehouse, iso3: Sequence[str]) -> pd.DataFrame:
    return wh.query(
        """
        SELECT country_iso3, country_name, indicator, years_with_value, years_expected,
               completeness_status, missing_years
        FROM marts.v_series_completeness WHERE list_contains(?, country_iso3)
        ORDER BY country_iso3, indicator
        """,
        [list(iso3)],
    )


def missing(wh: Warehouse, iso3: Sequence[str], year_from: int, year_to: int) -> pd.DataFrame:
    return wh.query(
        """
        SELECT country_name, indicator, year, missing_reason, gap_type
        FROM marts.v_missing_values
        WHERE list_contains(?, country_iso3) AND year BETWEEN ? AND ?
        ORDER BY country_iso3, indicator, year
        """,
        [list(iso3), year_from, year_to],
    )


def _empty(columns: list[str] | None = None) -> pd.DataFrame:
    # Annotated so the result is typed with every pandas-stubs version (3.x types the
    # DataFrame constructor and .head() as Any).
    frame: pd.DataFrame = pd.DataFrame(columns=columns)
    return frame


def dq_latest(wh: Warehouse) -> pd.DataFrame:
    return wh.query(
        """
        SELECT check_name, severity, status, violation_count, description, sample
        FROM quality.dq_result
        WHERE checked_at = (SELECT max(checked_at) FROM quality.dq_result)
        ORDER BY CASE status WHEN 'error' THEN 0 WHEN 'fail' THEN 1 WHEN 'warn' THEN 2 ELSE 3 END,
                 check_name
        """
    )


def dq_history(wh: Warehouse) -> pd.DataFrame:
    return wh.query(
        """
        SELECT checked_at, ingest_run_id,
               count(*) FILTER (WHERE status = 'pass') AS passed,
               count(*) FILTER (WHERE status = 'warn') AS warnings,
               count(*) FILTER (WHERE status IN ('fail', 'error')) AS blocking
        FROM quality.dq_result GROUP BY ALL ORDER BY checked_at DESC LIMIT 50
        """
    )


def runs(wh: Warehouse) -> pd.DataFrame:
    return wh.query(
        """
        SELECT run_id, status, started_at, finished_at, n_pages, n_new_responses, error
        FROM raw.ingest_run ORDER BY started_at DESC LIMIT 50
        """
    )


def revisions(wh: Warehouse, iso3: Sequence[str]) -> pd.DataFrame:
    return wh.query(
        """
        SELECT * FROM marts.v_source_revisions WHERE list_contains(?, country_iso3)
        ORDER BY detected_at DESC
        """,
        [list(iso3)],
    )


def lineage(wh: Warehouse, iso3: str, indicator: str, year: int) -> dict[str, Any] | None:
    """Trace one value back to the API response, request and landing file it came from."""
    df = wh.query(
        """
        SELECT f.value, f.value_status, f.response_id, f.first_loaded_run_id,
               f.last_changed_run_id, r.request_url, r.page, r.source_lastupdated,
               r.payload_sha256, r.first_fetched_at, r.landing_path,
               (SELECT o FROM (
                    SELECT unnest(CAST(json_extract(r.payload, '$[1]') AS JSON[])) AS o)
                WHERE (o ->> '$.countryiso3code') = f.country_iso3
                  AND (o ->> '$.indicator.id') = f.indicator_code
                  AND (o ->> '$.date') = CAST(f.year AS VARCHAR)
                LIMIT 1) AS source_row
        FROM core.fct_indicator_value f
        LEFT JOIN raw.api_response r USING (response_id)
        WHERE f.country_iso3 = ? AND f.indicator_code = ? AND f.year = ?
        """,
        [iso3, indicator, year],
    )
    return {str(k): v for k, v in df.iloc[0].items()} if len(df) else None


def run_sql(wh: Warehouse, sql: str) -> pd.DataFrame:
    """Analyst SQL. Safe because the connection is read-only; capped at MAX_SQL_ROWS rows."""
    sql = sql.strip().rstrip(";")
    if not sql:
        return _empty()
    try:
        return wh.query(f"SELECT * FROM ({sql}) LIMIT {MAX_SQL_ROWS}")
    except duckdb.ParserException:
        # Not a SELECT-shaped query (e.g. SHOW TABLES / DESCRIBE): run as-is.
        capped: pd.DataFrame = wh.query(sql).head(MAX_SQL_ROWS)
        return capped
