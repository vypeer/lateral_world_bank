"""Build the staging and core layers from one ingest run.

Configuration is materialised into the ``config`` schema first, so the SQL models derive the
expected country x indicator x year spine from configuration rather than from whatever the
API happened to return. All layers are rebuilt/upserted in a single transaction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import duckdb

from wbpipe.config import Settings
from wbpipe.ingest import AGGREGATE_REGION_ID
from wbpipe.warehouse import connect, sql_files

log = logging.getLogger(__name__)

LAYERS = ("staging", "core", "marts")


class TransformError(RuntimeError):
    pass


@dataclass(frozen=True)
class TransformResult:
    run_id: str
    fact_rows: int
    versions_opened: int
    versions_closed: int


def latest_successful_run(con: duckdb.DuckDBPyConnection) -> str:
    row = con.execute(
        "SELECT run_id FROM raw.ingest_run WHERE status = 'succeeded' "
        "ORDER BY finished_at DESC, run_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise TransformError("no successful ingest run found; run `wbpipe ingest` first")
    return str(row[0])


def load_config_tables(con: duckdb.DuckDBPyConnection, settings: Settings) -> None:
    con.execute("CREATE SCHEMA IF NOT EXISTS config")
    con.execute("CREATE OR REPLACE TABLE config.focus_country (country_iso3 VARCHAR)")
    if settings.focus_countries:
        con.executemany(
            "INSERT INTO config.focus_country VALUES (?)", [[c] for c in settings.focus_countries]
        )
    con.execute(
        "CREATE OR REPLACE TABLE config.indicator (indicator_code VARCHAR, short_name VARCHAR, "
        "config_order INTEGER, valid_min DOUBLE, valid_max DOUBLE, aggregation VARCHAR, "
        "agg_numerator VARCHAR, agg_denominator VARCHAR, agg_weight VARCHAR)"
    )
    con.executemany(
        "INSERT INTO config.indicator VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            [
                i.code, i.short_name, n, i.valid_min, i.valid_max, i.aggregation.method,
                i.aggregation.numerator, i.aggregation.denominator, i.aggregation.weight,
            ]
            for n, i in enumerate(settings.indicators, 1)
        ],
    )  # fmt: skip
    con.execute("CREATE OR REPLACE TABLE config.year (year INTEGER)")
    con.executemany("INSERT INTO config.year VALUES (?)", [[y] for y in settings.years])
    # Zero or one row; the consistency view and check are empty when the rule is not configured.
    con.execute(
        "CREATE OR REPLACE TABLE config.gdp_per_capita_rule (gdp_code VARCHAR, "
        "population_code VARCHAR, gdp_per_capita_code VARCHAR, tolerance DOUBLE)"
    )
    rule = settings.quality.gdp_per_capita
    if rule is not None:
        con.execute(
            "INSERT INTO config.gdp_per_capita_rule VALUES (?, ?, ?, ?)",
            [rule.gdp, rule.population, rule.gdp_per_capita, rule.tolerance],
        )


def load_country_scope(con: duckdb.DuckDBPyConnection, settings: Settings) -> None:
    """config.country: the configured list, or in 'all' mode every economy in the ingested
    country metadata (aggregates excluded). Focus countries come first in display order.

    Runs after staging because 'all' mode depends on what the API listed.
    """
    if settings.countries is not None:
        codes = list(settings.countries)
    else:
        rows = con.execute(
            "SELECT DISTINCT country_iso3 FROM staging.stg_country "
            "WHERE region_id IS DISTINCT FROM ? ORDER BY country_iso3",
            [AGGREGATE_REGION_ID],
        ).fetchall()
        codes = [str(r[0]) for r in rows]
    focus = set(settings.focus_countries)
    ordered = [c for c in settings.focus_countries if c in codes]
    ordered += [c for c in codes if c not in focus]
    con.execute(
        "CREATE OR REPLACE TABLE config.country "
        "(country_iso3 VARCHAR, config_order INTEGER, is_focus BOOLEAN)"
    )
    con.executemany(
        "INSERT INTO config.country VALUES (?, ?, ?)",
        [[code, n, code in focus] for n, code in enumerate(ordered, 1)],
    )


def wide_view_sql(settings: Settings) -> str:
    """marts.v_country_year_wide: one row per country x year, one column per indicator.

    Generated from configuration because DuckDB cannot use a dynamic PIVOT in a view. Codes and
    short names are validated by config (indicator-code pattern, snake_case identifiers).
    """
    columns = ",\n    ".join(
        f"max(value) FILTER (WHERE indicator_code = '{i.code}') AS \"{i.short_name}\""
        for i in settings.indicators
    )
    return f"""
CREATE OR REPLACE VIEW marts.v_country_year_wide AS
SELECT
    country_iso3,
    any_value(country_name) AS country_name,
    any_value(region_name) AS region_name,
    any_value(income_group_id) AS income_group_id,
    any_value(income_group_name) AS income_group_name,
    any_value(is_focus) AS is_focus,
    year,
    {columns},
    count(*) FILTER (WHERE NOT has_value) AS indicators_missing,
    any_value(country_order) AS country_order
FROM marts.v_indicator_value
GROUP BY country_iso3, year
"""


def run_transform(settings: Settings, run_id: str | None = None) -> TransformResult:
    """Transform the given ingest run (default: the latest successful one)."""
    transformed_at = datetime.now(timezone.utc)
    with connect(settings) as con:
        run_id = run_id or latest_successful_run(con)
        run = con.execute(
            "SELECT status, config_fingerprint FROM raw.ingest_run WHERE run_id = ?", [run_id]
        ).fetchone()
        if run is None or run[0] != "succeeded":
            raise TransformError(f"ingest run {run_id!r} does not exist or did not succeed")
        if run[1] != settings.fingerprint():
            log.warning(
                "config changed since ingest run %s; combinations it did not fetch will be "
                "'not_returned' until the next ingest",
                run_id,
            )

        log.info("transforming ingest run %s", run_id)
        con.execute("SET VARIABLE run_id = ?", [run_id])
        con.execute("SET VARIABLE transformed_at = ?::TIMESTAMPTZ", [transformed_at])
        con.execute("BEGIN TRANSACTION")
        try:
            load_config_tables(con, settings)
            for layer in LAYERS:
                for path in sql_files(settings, layer):
                    log.debug("executing %s", path.relative_to(settings.paths.sql_dir))
                    con.execute(path.read_text())
                if layer == "staging":
                    load_country_scope(con, settings)
            con.execute(wide_view_sql(settings))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        counts = con.execute(
            "SELECT fact_rows, versions_opened, versions_closed FROM core.transform_run "
            "WHERE transformed_at = ?",
            [transformed_at],
        ).fetchone()
    assert counts is not None
    result = TransformResult(run_id, int(counts[0]), int(counts[1]), int(counts[2]))
    log.info(
        "fact has %d rows; %d new version(s), %d closed version(s)",
        result.fact_rows, result.versions_opened, result.versions_closed,
    )  # fmt: skip
    return result
