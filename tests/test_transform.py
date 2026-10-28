from __future__ import annotations

import dataclasses
import json
import shutil

import duckdb
import pytest
from conftest import edit_observations, query

from wbpipe.config import Indicator
from wbpipe.transform import TransformError, run_transform

SPINE = 9 * 4 * 7


def fact(settings, key=("BRA", "SP.POP.TOTL", 2018)):
    rows = query(
        settings,
        "SELECT value, value_status, response_id, last_changed_run_id, changed_at "
        "FROM core.fct_indicator_value WHERE country_iso3 = ? AND indicator_code = ? AND year = ?",
        list(key),
    )
    return rows[0] if rows else None


def test_first_load_builds_dims_and_full_spine(settings, pipeline):
    result = pipeline("run1")
    assert (result.fact_rows, result.versions_opened, result.versions_closed) == (SPINE, SPINE, 0)
    assert query(
        settings, "SELECT count(*) FROM core.dim_country WHERE country_name IS NOT NULL"
    ) == [(9,)]
    assert query(
        settings, "SELECT income_group_id FROM core.dim_country WHERE country_iso3 = 'IND'"
    ) == [("LMC",)]
    assert query(
        settings, "SELECT region_name FROM core.dim_country WHERE country_iso3 = 'BRA'"
    ) == [
        ("Latin America & Caribbean",)  # source has a trailing space
    ]
    assert query(
        settings, "SELECT count(*) FROM core.dim_indicator WHERE indicator_name IS NOT NULL"
    ) == [(4,)]
    assert query(settings, "SELECT min(year), max(year), count(*) FROM core.dim_year") == [
        (2018, 2024, 7)
    ]
    statuses = query(
        settings, "SELECT value_status, count(*) FROM core.fct_indicator_value GROUP BY 1"
    )
    assert statuses == [("present", SPINE)]


def test_every_fact_value_matches_the_response_it_points_to(settings, pipeline):
    pipeline("run1")
    rows = query(
        settings,
        "SELECT f.country_iso3, f.indicator_code, f.year, f.value, r.payload::VARCHAR "
        "FROM core.fct_indicator_value f JOIN raw.api_response r USING (response_id)",
    )
    assert len(rows) == SPINE
    for iso3, code, year, value, payload in rows:
        (match,) = [
            o for o in json.loads(payload)[1]
            if (o["countryiso3code"], o["indicator"]["id"], o["date"]) == (iso3, code, str(year))
        ]  # fmt: skip
        assert match["value"] == value


def test_rerun_on_unchanged_source_rewrites_nothing(settings, pipeline):
    pipeline("run1")
    snapshot = query(settings, "SELECT * FROM core.fct_indicator_value ORDER BY ALL")
    result = pipeline("run2")
    assert (result.versions_opened, result.versions_closed) == (0, 0)
    assert query(settings, "SELECT * FROM core.fct_indicator_value ORDER BY ALL") == snapshot
    assert query(settings, "SELECT count(*) FROM core.fct_indicator_value_history") == [(SPINE,)]


def test_source_revision_is_applied_and_versioned(settings, pipeline, fake_session):
    pipeline("run1")
    untouched_before = fact(settings, ("ZAF", "SP.POP.TOTL", 2024))  # same page, not revised
    revised_key = ("ZAF", "SP.POP.TOTL", 2018)  # last row of page 2

    def revise(meta, rows):
        assert (rows[-1]["countryiso3code"], rows[-1]["date"]) == ("ZAF", "2018")
        rows[-1]["value"] = 1.0

    edit_observations(fake_session, "SP.POP.TOTL", revise)
    result = pipeline("run2")
    assert (result.versions_opened, result.versions_closed) == (1, 1)
    value, status, response_id, changed_run, _ = fact(settings, revised_key)
    assert (value, status, changed_run) == (1.0, "present", "run2")
    assert query(
        settings,
        "SELECT change_type, valid_from_run_id, valid_to_run_id, is_current "
        "FROM core.fct_indicator_value_history "
        "WHERE country_iso3 = 'ZAF' AND indicator_code = 'SP.POP.TOTL' AND year = 2018 "
        "ORDER BY valid_from",
    ) == [("initial", "run1", "run2", False), ("revision", "run2", None, True)]

    # A row on the same page with unchanged content keeps its version but follows the
    # new response for lineage.
    untouched_after = fact(settings, ("ZAF", "SP.POP.TOTL", 2024))
    assert untouched_after[3] == untouched_before[3] == "run1"
    assert untouched_after[4] == untouched_before[4]
    assert untouched_after[2] == response_id != untouched_before[2]


def test_null_value_in_source_is_kept_as_null_in_source(settings, pipeline, fake_session):
    def null_out(meta, rows):
        rows[-1]["value"] = None

    edit_observations(fake_session, "SP.POP.TOTL", null_out)
    pipeline("run1")
    value, status, response_id, *_ = fact(settings, ("ZAF", "SP.POP.TOTL", 2018))
    assert (value, status) == (None, "null_in_source")
    assert response_id is not None  # still traceable to the response that said "null"


def test_row_missing_from_source_is_not_returned(settings, pipeline, fake_session):
    def drop(meta, rows):
        rows.pop()
        meta["total"] -= 1

    edit_observations(fake_session, "SP.POP.TOTL", drop)
    pipeline("run1")
    assert query(settings, "SELECT count(*) FROM core.fct_indicator_value") == [(SPINE,)]
    value, status, response_id, *_ = fact(settings, ("ZAF", "SP.POP.TOTL", 2018))
    assert (value, status, response_id) == (None, "not_returned", None)


def test_duplicate_source_rows_do_not_break_the_grain(settings, pipeline, fake_session):
    def duplicate(meta, rows):
        rows.append(dict(rows[-1], value=999.0))
        meta["total"] += 1

    edit_observations(fake_session, "SP.POP.TOTL", duplicate)
    pipeline("run1")
    assert query(settings, "SELECT count(*) FROM core.fct_indicator_value") == [(SPINE,)]
    assert fact(settings, ("ZAF", "SP.POP.TOTL", 2018))[0] != 999.0  # first occurrence wins


def test_scope_follows_configuration(settings, ingest, caplog):
    ingest("run1")
    wider = dataclasses.replace(
        settings,
        countries=(*settings.countries, "FRA"),
        indicators=(*settings.indicators, Indicator("EN.ATM.CO2E.PC", "co2_per_capita")),
    )
    run_transform(wider)
    assert "config changed since ingest run run1" in caplog.text
    assert query(settings, "SELECT count(*) FROM core.fct_indicator_value") == [(10 * 5 * 7,)]
    assert query(
        settings,
        "SELECT count(*) FROM core.fct_indicator_value "
        "WHERE value_status = 'not_returned' "
        "AND (country_iso3 = 'FRA' OR indicator_code = 'EN.ATM.CO2E.PC')",
    ) == [(10 * 5 * 7 - SPINE,)]
    assert query(
        settings, "SELECT country_name FROM core.dim_country WHERE country_iso3 = 'FRA'"
    ) == [(None,)]

    narrower = dataclasses.replace(settings, countries=settings.countries[:-1])  # drop ZAF
    result = run_transform(narrower)
    assert result.fact_rows == 8 * 4 * 7
    assert query(
        settings, "SELECT count(*) FROM core.fct_indicator_value WHERE country_iso3 = 'ZAF'"
    ) == [(0,)]
    # ZAF history (incl. the extra indicator's rows) is closed without a successor, not deleted
    assert query(
        settings,
        "SELECT count(*) FROM core.fct_indicator_value_history WHERE country_iso3 = 'ZAF' "
        "AND NOT is_current AND valid_to IS NOT NULL",
    ) == [(5 * 7,)]


def test_older_run_can_be_replayed(settings, pipeline, fake_session):
    pipeline("run1")
    original = fact(settings, ("ZAF", "SP.POP.TOTL", 2018))[0]
    edit_observations(fake_session, "SP.POP.TOTL", lambda meta, rows: rows[-1].update(value=1.0))
    pipeline("run2")
    run_transform(settings, run_id="run1")
    assert fact(settings, ("ZAF", "SP.POP.TOTL", 2018))[0] == original
    assert query(
        settings,
        "SELECT count(*) FROM core.fct_indicator_value_history "
        "WHERE country_iso3 = 'ZAF' AND indicator_code = 'SP.POP.TOTL' AND year = 2018",
    ) == [(3,)]


def test_transform_requires_a_successful_run(settings, ingest):
    with pytest.raises(TransformError, match="no successful ingest run"):
        run_transform(settings)
    ingest("run1")
    with pytest.raises(TransformError, match="does not exist"):
        run_transform(settings, run_id="nope")


def test_failed_transform_leaves_previous_state_intact(settings, pipeline, tmp_path):
    pipeline("run1")
    snapshot = query(settings, "SELECT * FROM core.fct_indicator_value ORDER BY ALL")
    broken_sql = tmp_path / "sql"
    shutil.copytree(settings.paths.sql_dir, broken_sql)
    (broken_sql / "core" / "99_broken.sql").write_text("SELECT * FROM does_not_exist;")
    broken = dataclasses.replace(
        settings, paths=dataclasses.replace(settings.paths, sql_dir=broken_sql)
    )
    with pytest.raises(duckdb.CatalogException):
        run_transform(broken)
    assert query(settings, "SELECT * FROM core.fct_indicator_value ORDER BY ALL") == snapshot


def test_cli_replays_a_given_run(settings, pipeline, fake_session, monkeypatch):
    from wbpipe import cli

    pipeline("run1")
    edit_observations(fake_session, "SP.POP.TOTL", lambda meta, rows: rows[-1].update(value=1.0))
    pipeline("run2")
    monkeypatch.setattr(cli, "load_settings", lambda _path: settings)
    assert cli.main(["transform", "--run-id", "run1"]) == 0
    assert query(settings, "SELECT ingest_run_id FROM core.transform_run "
                           "ORDER BY transformed_at DESC LIMIT 1") == [("run1",)]  # fmt: skip
    assert fact(settings, ("ZAF", "SP.POP.TOTL", 2018))[0] != 1.0
    with pytest.raises(SystemExit):
        cli.main(["check", "--run-id", "run1"])
