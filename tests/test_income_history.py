"""Historical income groups from the World Bank's OGHIST workbook."""

from __future__ import annotations

import dataclasses
import io
import logging

import openpyxl
import pytest
from conftest import LIVE, FakeResponse, query

from wbpipe.income_history import IncomeHistoryError, parse_oghist
from wbpipe.quality import run_checks
from wbpipe.transform import run_transform

SHEET = "Country Analytical History"
REAL = (LIVE / "income_history_oghist.xlsx").read_bytes()


def workbook(groups: dict[str, dict[int, str]], years=range(2016, 2024)) -> bytes:
    """A minimal workbook in OGHIST's layout: {iso3: {data_year: code}}."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = SHEET
    ws.append([None, "World Bank Analytical Classifications"])
    ws.append([None, "Bank's fiscal year:", *[f"FY{(y + 2) % 100:02d}" for y in years]])
    ws.append([None, "Data for calendar year :", *years])
    ws.append([None, "Low income (L)", *["<= 1,000" for _ in years]])
    for iso3, by_year in groups.items():
        ws.append([iso3, iso3.title(), *[by_year.get(y) for y in years]])
    ws.append([None, "Note: footnote"])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


@pytest.fixture
def serve_history(settings, fake_session):
    def _serve(body: bytes) -> None:
        fake_session.override(settings.income_history.url, body)

    return _serve


# ---- parser ----------------------------------------------------------------------------------


def test_parses_the_real_workbook():
    records = parse_oghist(REAL, SHEET)
    by_key = {(r["country_iso3"], r["data_year"]): r for r in records}
    assert len({r["country_iso3"] for r in records}) == 223
    assert by_key[("BRA", 2018)] == {
        "country_iso3": "BRA", "data_year": 2018, "fiscal_year": "FY20",
        "source_code": "UM", "income_group_id": "UMC",
    }  # fmt: skip
    assert by_key[("IND", 2023)]["income_group_id"] == "LMC"
    assert by_key[("VEN", 2023)]["income_group_id"] == "INX"  # unclassified since 2021
    assert (min(r["data_year"] for r in records), max(r["data_year"] for r in records)) == (
        1987,
        2025,
    )


@pytest.mark.parametrize(
    ("body", "sheet", "message"),
    [
        (b"<html>not a workbook</html>", SHEET, "not a readable"),
        (REAL, "Some Other Sheet", "not found"),
        (workbook({}), SHEET, "no economy rows"),
    ],
)
def test_unexpected_workbooks_are_rejected(body, sheet, message):
    with pytest.raises(IncomeHistoryError, match=message):
        parse_oghist(body, sheet)


# ---- warehouse --------------------------------------------------------------------------------


def income(settings, iso3, year):
    return query(
        settings,
        "SELECT income_group_id, income_group_source, fiscal_year "
        "FROM core.dim_country_income_year WHERE country_iso3 = ? AND year = ?",
        [iso3, year],
    )[0]


def test_groups_come_from_the_historical_classification(settings, pipeline):
    pipeline("run1")
    assert income(settings, "BRA", 2018) == ("UMC", "historical", "FY20")
    assert income(settings, "IND", 2023) == ("LMC", "historical", "FY25")
    assert income(settings, "BRA", 2024) == ("UMC", "historical", "FY26")
    landed = settings.paths.landing_dir / "run1" / "income_history"
    assert (landed / "OGHIST.xlsx").read_bytes() == REAL  # verbatim
    assert query(settings, "SELECT count(*) FROM core.dim_country_income_year") == [(9 * 7,)]


def test_years_beyond_the_workbook_use_the_current_group(settings, pipeline, serve_history):
    serve_history(workbook({"BRA": {2023: "UM"}, "IND": {2023: "LM"}}))  # ends at data year 2023
    pipeline("run1")
    assert income(settings, "BRA", 2023) == ("UMC", "historical", "FY25")
    assert income(settings, "BRA", 2024) == ("UMC", "current", None)  # flagged fallback


def test_income_group_marts_follow_each_years_group(settings, pipeline, serve_history):
    # Brazil high income in 2019 only (synthetic): it moves group for that year
    serve_history(workbook({"BRA": {2018: "UM", 2019: "H", 2020: "UM"}, "IND": {2019: "LM"}}))
    pipeline("run1")
    rows = query(
        settings,
        "SELECT year, income_group_id FROM marts.v_indicator_value "
        "WHERE country_iso3 = 'BRA' AND indicator = 'population' AND year <= 2020 ORDER BY year",
    )
    assert rows == [(2018, "UMC"), (2019, "HIC"), (2020, "UMC")]
    members = query(
        settings,
        "SELECT countries FROM marts.v_income_group_by_year "
        "WHERE income_group_id = 'HIC' AND year = 2019 AND indicator = 'population'",
    )[0][0]
    assert "BRA" in members.split(";")
    current = query(settings, "SELECT current_income_group_id FROM marts.v_indicator_value "
                              "WHERE country_iso3 = 'BRA' AND year = 2019 LIMIT 1")  # fmt: skip
    assert current == [("UMC",)]


def test_not_classified_is_kept_as_its_own_group(settings, pipeline, serve_history):
    serve_history(workbook({"BRA": {2020: "UM", 2021: None}, "IND": {2021: "LM"}}))
    pipeline("run1")
    assert income(settings, "BRA", 2021)[0] == "INX"
    name = query(settings, "SELECT DISTINCT income_group_name FROM marts.v_indicator_value "
                           "WHERE country_iso3 = 'BRA' AND year = 2021")  # fmt: skip
    assert name == [("Not classified",)]


def test_unavailable_workbook_falls_back_and_warns(settings, pipeline, fake_session, caplog):
    fake_session.queued[settings.income_history.url].append(FakeResponse(404, b"gone"))
    with caplog.at_level(logging.WARNING):
        pipeline("run1")
    assert "income history unavailable" in caplog.text
    sources = query(
        settings, "SELECT DISTINCT income_group_source FROM core.dim_country_income_year"
    )
    assert sources == [("current",)]
    run_checks(settings)  # a warning, not a failure
    warned = query(settings, "SELECT check_name FROM quality.dq_result WHERE status = 'warn'")
    assert ("income_history_loaded",) in warned


def test_history_can_be_disabled(settings, ingest_with, fake_session):
    off = dataclasses.replace(settings, income_history=None)
    ingest_with(off, "run1")
    assert not any("OGHIST" in url for url in fake_session.calls)
    run_transform(off)
    sources = query(
        settings, "SELECT DISTINCT income_group_source FROM core.dim_country_income_year"
    )
    assert sources == [("current",)]
    run_checks(off)  # no workbook expected, so no warning
    not_passing = query(settings, "SELECT count(*) FROM quality.dq_result WHERE status <> 'pass'")
    assert not_passing == [(0,)]


def test_a_superseded_workbook_is_flagged(settings, pipeline, serve_history):
    serve_history(workbook({"BRA": {2020: "UM"}, "IND": {2020: "LM"}}, years=range(2016, 2021)))
    pipeline("run1")
    run_checks(settings)
    warned = query(settings, "SELECT check_name FROM quality.dq_result WHERE status = 'warn'")
    assert ("income_history_up_to_date",) in warned
