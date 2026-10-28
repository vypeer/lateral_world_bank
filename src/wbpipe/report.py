"""Export mart views to CSV under output/.

Outputs are only published for a core layer that passed its data-quality checks: the latest
check execution must be for the latest transform and contain no blocking result.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from wbpipe.config import Settings
from wbpipe.warehouse import connect

log = logging.getLogger(__name__)

# Mart view -> CSV file name, in the order the reporting questions are asked.
EXPORTS = {
    "v_missing_values": "q1_missing_values.csv",
    "v_series_completeness": "q1_series_completeness.csv",
    "v_yoy_change": "q2_yoy_change.csv",
    "v_latest_comparison": "q3_latest_comparison.csv",
    "v_income_group_by_year": "q4_income_group_by_year.csv",
    "v_indicator_value": "indicator_values.csv",
    "v_country_year_wide": "country_year_wide.csv",
    "v_data_freshness": "data_freshness.csv",
    "v_source_revisions": "source_revisions.csv",
}

# Deterministic row order per view, so re-exports of unchanged data produce identical files.
ORDER_BY = {
    "v_missing_values": "country_iso3, indicator_code, year",
    "v_series_completeness": "country_iso3, indicator_code",
    "v_yoy_change": "country_iso3, indicator_code, year",
    "v_latest_comparison": "indicator_code, rank_at_common_year NULLS LAST, country_iso3",
    "v_income_group_by_year": "indicator_code, year, income_group_rank NULLS LAST",
    "v_indicator_value": "country_order, indicator_order, year",
    "v_country_year_wide": "country_order, year",
    "v_data_freshness": "indicator_code",
    "v_source_revisions": "detected_at, country_iso3, indicator_code, year",
}


class ReportError(RuntimeError):
    pass


def assert_quality_gate(con: duckdb.DuckDBPyConnection) -> None:
    try:
        row = con.execute(
            """
            WITH latest_check AS (
                SELECT max(checked_at) AS checked_at FROM quality.dq_result
            )
            SELECT
                (SELECT max(transformed_at) FROM core.transform_run)
                    <= (SELECT checked_at FROM latest_check),
                (SELECT list(check_name ORDER BY check_name) FROM quality.dq_result
                 WHERE checked_at = (SELECT checked_at FROM latest_check)
                   AND status IN ('fail', 'error'))
            """
        ).fetchone()
    except duckdb.CatalogException:
        row = None
    if row is None or not row[0]:
        raise ReportError("the current core layer has not been checked: run `wbpipe check` first")
    if row[1]:
        raise ReportError(f"not publishing: blocking data-quality checks failed: {row[1]}")


def run_report(settings: Settings) -> list[Path]:
    out_dir = settings.paths.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    with connect(settings) as con:
        assert_quality_gate(con)
        for view, filename in EXPORTS.items():
            path = out_dir / filename
            con.execute(
                f"COPY (SELECT * FROM marts.{view} ORDER BY {ORDER_BY[view]}) "
                f"TO '{path.as_posix()}' (HEADER, DELIMITER ',')"
            )
            rows = con.execute(f"SELECT count(*) FROM marts.{view}").fetchone()
            log.info("wrote %s (%d rows)", path.name, rows[0] if rows else 0)
            written.append(path)
    return written
