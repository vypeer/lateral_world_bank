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
    con.execute(
        "CREATE OR REPLACE TABLE config.country (country_iso3 VARCHAR, config_order INTEGER)"
    )
    con.executemany(
        "INSERT INTO config.country VALUES (?, ?)",
        [[code, n] for n, code in enumerate(settings.countries, 1)],
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
