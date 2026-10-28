"""Each check must pass on clean data and demonstrably catch the defect it exists for."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import duckdb
import pytest
from conftest import LIVE, FakeResponse, edit_observations, query

from wbpipe import cli
from wbpipe.api_client import WorldBankApiError
from wbpipe.config import Settings
from wbpipe.quality import DataQualityError, load_checks, run_checks
from wbpipe.transform import run_transform


def statuses(settings: Settings) -> dict[str, str]:
    """Status per check from the latest check execution."""
    rows = query(
        settings,
        "SELECT check_name, status FROM quality.dq_result "
        "WHERE checked_at = (SELECT max(checked_at) FROM quality.dq_result)",
    )
    return dict(rows)


def not_passing(settings: Settings) -> dict[str, str]:
    return {k: v for k, v in statuses(settings).items() if v != "pass"}


def run_expecting_failure(settings: Settings) -> dict[str, str]:
    with pytest.raises(DataQualityError, match="blocking data-quality checks failed"):
        run_checks(settings)
    return not_passing(settings)


def tamper(settings: Settings, sql: str) -> None:
    with duckdb.connect(str(settings.paths.warehouse)) as con:
        con.execute(sql)


def test_all_checks_pass_on_clean_data(settings, pipeline):
    pipeline("run1")
    results = run_checks(settings)
    assert len(results) == len(load_checks(settings.quality.checks_file))
    assert not_passing(settings) == {}
    report = (settings.paths.output_dir / "dq_report.md").read_text()
    assert "Ingest run checked: `run1`" in report
    csv_lines = (settings.paths.output_dir / "dq_results.csv").read_text().splitlines()
    assert len(csv_lines) == len(results) + 1


def test_results_accumulate_across_executions(settings, pipeline):
    pipeline("run1")
    run_checks(settings)
    run_checks(settings)
    n = len(load_checks(settings.quality.checks_file))
    assert query(
        settings, "SELECT count(DISTINCT checked_at), count(*) FROM quality.dq_result"
    ) == [(2, 2 * n)]


# ---- defects arriving from the source -----------------------------------------------------


def test_missing_values_warn_but_do_not_block(settings, pipeline, fake_session):
    edit_observations(fake_session, "SP.POP.TOTL", lambda meta, rows: rows[-1].update(value=None))
    pipeline("run1")
    run_checks(settings)  # no exception: warnings only
    assert not_passing(settings) == {"values_missing": "warn"}


def test_duplicate_source_rows_fail(settings, pipeline, fake_session):
    def duplicate(meta, rows):
        rows.append(dict(rows[-1]))
        meta["total"] += 1

    edit_observations(fake_session, "SP.POP.TOTL", duplicate)
    pipeline("run1")
    assert run_expecting_failure(settings) == {"source_duplicate_observations": "fail"}


def test_unparseable_value_fails(settings, pipeline, fake_session):
    edit_observations(fake_session, "SP.POP.TOTL", lambda meta, rows: rows[-1].update(value="n/a"))
    pipeline("run1")
    failed = run_expecting_failure(settings)
    assert failed["source_unparseable_values"] == "fail"
    assert failed["fct_status_consistent"] == "fail"  # 'present' but no numeric value


def test_out_of_range_value_fails(settings, pipeline, fake_session):
    edit_observations(fake_session, "SP.DYN.LE00.IN", lambda meta, rows: rows[0].update(value=150))
    pipeline("run1")
    assert run_expecting_failure(settings) == {"value_in_valid_range": "fail"}


def test_inconsistent_gdp_per_capita_warns(settings, pipeline, fake_session):
    edit_observations(
        fake_session,
        "NY.GDP.PCAP.CD",
        lambda meta, rows: rows[0].update(value=rows[0]["value"] * 1.05),
    )
    pipeline("run1")
    run_checks(settings)
    assert not_passing(settings) == {"gdp_per_capita_consistent": "warn"}
    assert query(
        settings,
        "SELECT count(*) FROM quality.v_gdp_per_capita_consistency "
        "WHERE consistency_status = 'inconsistent'",
    ) == [(1,)]


def test_tolerance_is_configurable(settings, pipeline, fake_session):
    edit_observations(
        fake_session,
        "NY.GDP.PCAP.CD",
        lambda meta, rows: rows[0].update(value=rows[0]["value"] * 1.05),
    )
    rule = dataclasses.replace(settings.quality.gdp_per_capita, tolerance=0.1)
    loose = dataclasses.replace(
        settings, quality=dataclasses.replace(settings.quality, gdp_per_capita=rule)
    )
    pipeline("run1")
    run_transform(loose)
    run_checks(loose)
    assert not_passing(loose) == {}


def test_observations_outside_scope_warn(settings, pipeline, fake_session):
    def add_france(meta, rows):
        rows.append(dict(rows[-1], countryiso3code="FRA", country={"id": "FR", "value": "France"}))
        meta["total"] += 1

    edit_observations(fake_session, "SP.POP.TOTL", add_france)
    pipeline("run1")
    run_checks(settings)
    assert not_passing(settings) == {"source_unexpected_observations": "warn"}


def test_configured_country_unknown_to_api_fails(settings, ingest):
    ingest("run1")
    wider = dataclasses.replace(settings, countries=(*settings.countries, "FRA"))
    run_transform(wider)
    assert run_expecting_failure(wider) == {
        "dim_country_described": "fail",
        "values_missing": "warn",
    }


def test_stale_source_warns(settings, pipeline):
    pipeline("run1")
    strict = dataclasses.replace(
        settings, quality=dataclasses.replace(settings.quality, max_source_age_days=-1)
    )
    run_checks(strict)
    assert not_passing(strict) == {"source_freshness": "warn"}


def test_failed_ingest_after_checked_run_warns(settings, pipeline, ingest, fake_session):
    pipeline("run1")
    fake_session.queued["indicator/SP.POP.TOTL"].append(
        FakeResponse(200, (LIVE / "error_invalid_indicator.json").read_bytes())
    )
    with pytest.raises(WorldBankApiError):
        ingest("run2")
    run_checks(settings)
    assert not_passing(settings) == {"ingest_failed_since_checked_run": "warn"}


# ---- defects inside the warehouse (guards against pipeline bugs) --------------------------


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "DELETE FROM core.fct_indicator_value WHERE country_iso3 = 'BRA' AND year = 2018 "
            "AND indicator_code = 'SP.POP.TOTL'",
            {"fct_spine_complete": "fail"},
        ),
        (
            "UPDATE core.fct_indicator_value SET response_id = 'bogus' WHERE country_iso3 = 'BRA'",
            {"fct_lineage_resolves": "fail"},
        ),
        ("DELETE FROM core.dim_year WHERE year = 2018", {"fct_dimension_integrity": "fail"}),
        (
            "UPDATE core.fct_indicator_value SET value = NULL WHERE country_iso3 = 'BRA' "
            "AND year = 2018 AND indicator_code = 'SP.DYN.LE00.IN'",
            {"fct_status_consistent": "fail"},
        ),
    ],
)
def test_corrupted_core_fails(settings, pipeline, sql, expected):
    pipeline("run1")
    tamper(settings, sql)
    assert run_expecting_failure(settings) == expected


def test_check_with_broken_sql_is_an_error(settings, pipeline, tmp_path):
    pipeline("run1")
    checks_file = tmp_path / "checks.yaml"
    checks_file.write_text(
        "checks:\n  - {name: broken, severity: warn, description: d, sql: SELECT * FROM nowhere}\n"
    )
    broken = dataclasses.replace(
        settings, quality=dataclasses.replace(settings.quality, checks_file=checks_file)
    )
    # Even a warn-level check blocks when it cannot run: an unknown result is not a pass.
    assert run_expecting_failure(broken) == {"broken": "error"}
    assert "CatalogException" in (settings.paths.output_dir / "dq_report.md").read_text()


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("checks:\n  - {name: a, severity: fatal, description: d, sql: SELECT 1}\n", "severity"),
        ("checks:\n  - {name: a, severity: warn, description: d}\n", "missing"),
        (
            "checks:\n  - {name: a, severity: warn, description: d, sql: SELECT 1}\n"
            "  - {name: a, severity: warn, description: d, sql: SELECT 1}\n",
            "duplicate",
        ),
        ("checks: []\n", "no checks"),
    ],
)
def test_invalid_check_definitions_are_rejected(tmp_path, body, message):
    path = tmp_path / "checks.yaml"
    path.write_text(body)
    with pytest.raises(ValueError, match=message):
        load_checks(path)


def test_checks_require_a_transform(settings, ingest):
    ingest("run1")
    with pytest.raises(DataQualityError, match="run `wbpipe transform` first"):
        run_checks(settings)


def test_cli_exits_non_zero_on_blocking_failure(settings, pipeline, monkeypatch):
    pipeline("run1")
    tamper(settings, "DELETE FROM core.dim_year WHERE year = 2018")
    monkeypatch.setattr(cli, "load_settings", lambda _path: settings)
    assert cli.main(["check"]) == 1


def test_dq_result_timestamps_are_utc(settings, pipeline):
    pipeline("run1")
    run_checks(settings)
    (checked_at,) = query(settings, "SELECT max(checked_at) FROM quality.dq_result")[0]
    assert abs(checked_at - datetime.now(timezone.utc)).total_seconds() < 60
