"""The live app: data layer queries, and the Streamlit UI driven headlessly with AppTest."""

from __future__ import annotations

import subprocess
from pathlib import Path

import duckdb
import pytest
from conftest import edit_observations

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest

from wbpipe import app_data as d
from wbpipe.quality import run_checks

APP = str(Path(__file__).resolve().parents[1] / "src" / "wbpipe" / "app.py")
ALL = ("BRA", "CAN", "DEU", "GBR", "IND", "JPN", "MEX", "USA", "ZAF")


@pytest.fixture
def built(settings, pipeline):
    pipeline("run1")
    run_checks(settings)
    return d.Warehouse(settings.paths.warehouse)


@pytest.fixture
def app(built, monkeypatch):
    monkeypatch.setenv("WBPIPE_WAREHOUSE", str(built.path))
    return AppTest.from_file(APP, default_timeout=60)


# ---- data layer ------------------------------------------------------------------------------


def test_status_reports_build_and_checks(built):
    status = d.status(built)
    assert status["ingest_run_id"] == "run1"
    assert status["dq"] == {"pass": 16, "warn": 0, "fail": 0, "error": 0}


def test_comparison_uses_latest_year_common_to_the_selection(settings, ingest, fake_session):
    from wbpipe.transform import run_transform

    def drop_zaf_2024(meta, rows):
        # ZAF's seven rows (2024 -> 2018) close page 2
        assert (rows[-7]["countryiso3code"], rows[-7]["date"]) == ("ZAF", "2024")
        rows.pop(-7)
        meta["total"] -= 1

    edit_observations(fake_session, "NY.GDP.PCAP.CD", drop_zaf_2024)
    ingest("run1")
    run_transform(settings)
    wh = d.Warehouse(settings.paths.warehouse)
    year, df = d.comparison(wh, "NY.GDP.PCAP.CD", ["USA", "IND"], None)
    assert year == 2024 and list(df["country_iso3"]) == ["USA", "IND"]
    year, df = d.comparison(wh, "NY.GDP.PCAP.CD", ["USA", "ZAF"], None)
    assert year == 2023
    assert df["pct_of_selected_total"].sum() == pytest.approx(100)


def test_filters_are_applied_as_parameters(built):
    df = d.values(built, "SP.POP.TOTL", ["JPN", "IND"], 2020, 2022)
    assert set(df["country_iso3"]) == {"JPN", "IND"} and set(df["year"]) == {2020, 2021, 2022}
    # a hostile "country code" is just a value that matches nothing
    assert d.values(built, "SP.POP.TOTL", ["x' OR 1=1 --"], 2018, 2024).empty


def test_lineage_returns_the_exact_source_row(built):
    info = d.lineage(built, "JPN", "SP.POP.TOTL", 2024)
    assert info is not None
    assert "indicator/SP.POP.TOTL" in info["request_url"]
    assert info["landing_path"].startswith("run1/observation/SP.POP.TOTL/")
    assert '"countryiso3code":"JPN"' in info["source_row"] and '"date":"2024"' in info["source_row"]


def test_sql_is_read_only_and_capped(built):
    assert d.run_sql(built, "SELECT count(*) AS n FROM core.fct_indicator_value;")["n"][0] == 252
    assert len(d.run_sql(built, "SELECT * FROM range(100000)")) == d.MAX_SQL_ROWS
    for statement in ("DELETE FROM core.dim_year", "DROP TABLE core.dim_year"):
        with pytest.raises(duckdb.InvalidInputException):
            d.run_sql(built, statement)
    assert len(d.years(built)) == 7


def test_unavailable_warehouse_is_explained(tmp_path, built):
    with pytest.raises(d.WarehouseUnavailable, match="run `make run`"):
        d.status(d.Warehouse(tmp_path / "missing.duckdb"))
    # a writer holds the database
    with (
        duckdb.connect(str(built.path)),
        pytest.raises(d.WarehouseUnavailable, match="being updated"),
    ):
        d.status(built)


# ---- UI --------------------------------------------------------------------------------------


def test_app_renders_every_tab(app):
    at = app.run()
    assert not at.exception
    assert [t.label for t in at.tabs] == [
        "Trends", "Compare", "Year-over-year", "Income groups", "Data quality & lineage",
        "Explore & SQL",
    ]  # fmt: skip
    assert any("16/16 data-quality checks pass" in s.value for s in at.success)


def test_filters_drive_every_view(app):
    at = app.run()
    countries = next(m for m in at.multiselect if m.label.startswith("Countries"))
    countries.set_value(["JPN", "IND"]).run()
    next(s for s in at.selectbox if s.label == "Indicator").set_value("SP.DYN.LE00.IN").run()
    next(s for s in at.slider if s.label == "Years").set_value((2020, 2022)).run()
    assert not at.exception
    lineage_country = next(s for s in at.selectbox if s.key == "lineage_country")
    assert lineage_country.options == ["Japan", "India"]


def test_no_country_selected_is_handled(app):
    at = app.run()
    next(m for m in at.multiselect if m.label.startswith("Countries")).set_value([]).run()
    assert not at.exception
    assert any("Select at least one country" in i.value for i in at.info)


def test_sql_box_shows_results_and_errors(app):
    at = app.run()
    at.text_area[0].set_value("SELECT 42 AS answer").run()
    next(b for b in at.button if b.label == "Run query").click().run()
    assert not at.exception and at.dataframe[-1].value["answer"][0] == 42
    at.text_area[0].set_value("DELETE FROM core.dim_year").run()
    next(b for b in at.button if b.label == "Run query").click().run()
    assert any("Cannot execute" in e.value for e in at.error)


def test_refresh_runs_the_pipeline(app, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ingest run x succeeded\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    at = app.run()
    next(b for b in at.button if b.label == "Refresh data").click().run()
    assert calls and calls[0][-1] == "run" and "--config" in calls[0]
    assert any("Pipeline run succeeded" in e.label for e in at.expander)


def test_missing_warehouse_shows_guidance(tmp_path, monkeypatch):
    monkeypatch.setenv("WBPIPE_WAREHOUSE", str(tmp_path / "none.duckdb"))
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert any("no warehouse" in w.value for w in at.warning)


def test_focus_countries_are_preselected(settings, ingest, monkeypatch):
    import dataclasses

    from wbpipe.transform import run_transform

    ingest("run1")
    run_transform(dataclasses.replace(settings, countries=(*settings.countries, "FRA")))
    monkeypatch.setenv("WBPIPE_WAREHOUSE", str(settings.paths.warehouse))
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    countries = next(m for m in at.multiselect if m.label.startswith("Countries"))
    assert len(countries.options) == 10 and countries.value == list(ALL)
    assert countries.label.startswith("Countries (10 in scope")
