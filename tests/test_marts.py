"""Mart views answer the reporting questions correctly, including around missing data.

Live data currently has no gaps, so gaps are created by editing the captured responses.
Page 1 of each indicator starts with BRA 2024, 2023, ... 2018 (index = 2024 - year).
"""

from __future__ import annotations

import dataclasses
import json
import math

import duckdb
import pytest
from conftest import LIVE, edit_observations, query

from wbpipe.config import Aggregation, Settings
from wbpipe.transform import run_transform

POP, GDP, GDPPC, LIFE = "SP.POP.TOTL", "NY.GDP.MKTP.CD", "NY.GDP.PCAP.CD", "SP.DYN.LE00.IN"


def live_value(indicator: str, iso3: str, year: int) -> float:
    for page in (1, 2):
        _, rows = json.loads((LIVE / f"observations_{indicator}_page{page}.json").read_bytes())
        for r in rows:
            if (r["countryiso3code"], r["date"]) == (iso3, str(year)):
                return float(r["value"])
    raise KeyError((indicator, iso3, year))


def bra(year: int) -> int:
    """Index of BRA <year> on page 1 (also each country's offset within its 7-row block)."""
    return 2024 - year


def set_bra(fake_session, indicator: str, year: int, value) -> None:
    def edit(meta, rows):
        assert (rows[bra(year)]["countryiso3code"], rows[bra(year)]["date"]) == ("BRA", str(year))
        rows[bra(year)]["value"] = value

    edit_observations(fake_session, indicator, edit, page=1)


def drop_row(fake_session, indicator: str, iso3: str, year: int) -> None:
    def edit(meta, rows):
        (idx,) = [
            n for n, r in enumerate(rows) if (r["countryiso3code"], r["date"]) == (iso3, str(year))
        ]
        rows.pop(idx)
        meta["total"] -= 1

    # 63 rows ordered BRA..ZAF, 2024..2018: the first 50 are on page 1
    countries = ["BRA", "CAN", "DEU", "GBR", "IND", "JPN", "MEX", "USA", "ZAF"]
    position = countries.index(iso3) * 7 + bra(year)
    edit_observations(fake_session, indicator, edit, page=1 if position < 50 else 2)


def mart(settings: Settings, view: str, where: str = "TRUE", order: str = "ALL") -> list[dict]:
    with duckdb.connect(str(settings.paths.warehouse), read_only=True) as con:
        rel = con.sql(f"SELECT * FROM marts.{view} WHERE {where} ORDER BY {order}")
        return [dict(zip(rel.columns, row, strict=True)) for row in rel.fetchall()]


# ---- Q1: missing / incomplete ---------------------------------------------------------------


def test_missing_values_explain_each_gap(settings, ingest, fake_session):
    set_bra(fake_session, POP, 2020, None)  # gap inside the series, null in source
    drop_row(fake_session, POP, "BRA", 2024)  # trailing gap, not returned
    ingest("run1")
    run_transform(dataclasses.replace(settings, countries=(*settings.countries, "FRA")))

    rows = mart(settings, "v_missing_values", "country_iso3 = 'BRA'")
    assert [(r["year"], r["missing_reason"], r["gap_type"]) for r in rows] == [
        (2020, "null_in_source", "gap_in_series"),
        (2024, "not_returned", "not_yet_published"),
    ]
    fra = mart(settings, "v_missing_values", "country_iso3 = 'FRA'")
    assert len(fra) == 4 * 7 and {r["gap_type"] for r in fra} == {"no_data_for_series"}

    (series,) = mart(
        settings, "v_series_completeness", f"country_iso3 = 'BRA' AND indicator_code = '{POP}'"
    )
    assert series["completeness_status"] == "incomplete"
    assert (
        series["years_with_value"],
        series["years_null_in_source"],
        series["years_not_returned"],
    ) == (5, 1, 1)
    assert series["missing_years"] == "2020;2024"
    assert series["last_year_with_value"] == 2023
    statuses = {
        r["completeness_status"]
        for r in mart(settings, "v_series_completeness", "country_iso3 = 'FRA'")
    }
    assert statuses == {"empty"}


# ---- Q2: year-over-year -----------------------------------------------------------------------


def yoy(settings, iso3: str, indicator: str, year: int) -> dict:
    (row,) = mart(
        settings, "v_yoy_change",
        f"country_iso3 = '{iso3}' AND indicator_code = '{indicator}' AND year = {year}",
    )  # fmt: skip
    return row


def test_strict_yoy_on_complete_data(settings, pipeline):
    pipeline("run1")
    v18, v19 = live_value(POP, "BRA", 2018), live_value(POP, "BRA", 2019)
    row = yoy(settings, "BRA", POP, 2019)
    assert row["yoy_status"] == "ok"
    assert row["yoy_abs_change"] == pytest.approx(v19 - v18)
    assert row["yoy_pct_change"] == pytest.approx(100 * (v19 - v18) / v18)
    assert row["annualized_pct_change_since_last_available"] == pytest.approx(row["yoy_pct_change"])
    first = yoy(settings, "BRA", POP, 2018)
    assert first["yoy_status"] == "prior_out_of_scope" and first["yoy_pct_change"] is None


def test_yoy_with_missing_prior_falls_back_to_gap_aware(settings, pipeline, fake_session):
    set_bra(fake_session, POP, 2020, None)
    pipeline("run1")
    missing = yoy(settings, "BRA", POP, 2020)
    assert missing["yoy_status"] == "current_missing"
    assert missing["yoy_abs_change"] is None and missing["last_available_year"] is None

    after = yoy(settings, "BRA", POP, 2021)
    assert after["yoy_status"] == "prior_missing"
    assert after["yoy_abs_change"] is None and after["yoy_pct_change"] is None  # never 0
    v19, v21 = live_value(POP, "BRA", 2019), live_value(POP, "BRA", 2021)
    assert (after["last_available_year"], after["years_since_last_available"]) == (2019, 2)
    assert after["abs_change_since_last_available"] == pytest.approx(v21 - v19)
    assert after["annualized_pct_change_since_last_available"] == pytest.approx(
        100 * (math.sqrt(v21 / v19) - 1)
    )


def test_yoy_with_zero_prior_keeps_absolute_change_only(settings, pipeline, fake_session):
    set_bra(fake_session, POP, 2020, 0)
    pipeline("run1")
    row = yoy(settings, "BRA", POP, 2021)
    assert row["yoy_status"] == "prior_zero"
    assert row["yoy_abs_change"] == pytest.approx(live_value(POP, "BRA", 2021))
    assert row["yoy_pct_change"] is None
    assert row["annualized_pct_change_since_last_available"] is None


# ---- Q3: latest-year comparison ---------------------------------------------------------------


def test_latest_comparison_uses_common_year_and_labels_country_latest(
    settings, ingest, fake_session
):
    drop_row(fake_session, GDPPC, "ZAF", 2024)
    ingest("run1")
    run_transform(settings)
    rows = mart(
        settings, "v_latest_comparison", f"indicator_code = '{GDPPC}'", "rank_at_common_year"
    )
    assert {r["latest_common_year"] for r in rows} == {2023}
    expected = sorted(
        ((live_value(GDPPC, r["country_iso3"], 2023), r["country_iso3"]) for r in rows),
        reverse=True,
    )
    assert [r["country_iso3"] for r in rows] == [iso3 for _, iso3 in expected]
    assert [r["rank_at_common_year"] for r in rows] == list(range(1, 10))
    assert sum(r["pct_of_selected_total_at_common_year"] for r in rows) == pytest.approx(100)
    zaf = next(r for r in rows if r["country_iso3"] == "ZAF")
    usa = next(r for r in rows if r["country_iso3"] == "USA")
    assert (zaf["country_latest_year"], zaf["is_same_as_common_year"]) == (2023, True)
    assert (usa["country_latest_year"], usa["is_same_as_common_year"]) == (2024, False)
    assert usa["country_latest_value"] == live_value(GDPPC, "USA", 2024)
    # other indicators are unaffected
    assert {
        r["latest_common_year"]
        for r in mart(settings, "v_latest_comparison", f"indicator_code = '{POP}'")
    } == {2024}


def test_common_year_is_anchored_on_focus_countries(settings, ingest):
    ingest("run1")
    # FRA is in scope but not a focus country and has no data: it is simply unranked
    run_transform(dataclasses.replace(settings, countries=(*settings.countries, "FRA")))
    rows = mart(settings, "v_latest_comparison", f"indicator_code = '{POP}'")
    assert len(rows) == 10 and {r["latest_common_year"] for r in rows} == {2024}
    fra = next(r for r in rows if r["country_iso3"] == "FRA")
    assert (fra["rank_at_common_year"], fra["is_focus"], fra["country_latest_year"]) == (
        None,
        False,
        None,
    )
    assert sorted(r["rank_at_common_year"] for r in rows if r["is_focus"]) == list(range(1, 10))


def test_latest_comparison_without_a_common_year(settings, ingest):
    ingest("run1")
    # a FOCUS country without data means no year in which every focus country has a value
    run_transform(
        dataclasses.replace(
            settings,
            countries=(*settings.countries, "FRA"),
            focus_countries=(*settings.focus_countries, "FRA"),
        )
    )
    rows = mart(settings, "v_latest_comparison", f"indicator_code = '{POP}'")
    assert len(rows) == 10
    assert {r["latest_common_year"] for r in rows} == {None}
    assert {r["rank_at_common_year"] for r in rows} == {None}
    assert next(r for r in rows if r["country_iso3"] == "USA")["country_latest_year"] == 2024


# ---- Q4: income groups --------------------------------------------------------------------------


def group(settings, income_group: str, indicator: str, year: int) -> dict:
    (row,) = mart(
        settings, "v_income_group_by_year",
        f"income_group_id = '{income_group}' AND indicator_code = '{indicator}' AND year = {year}",
    )  # fmt: skip
    return row


HIC = ("CAN", "DEU", "GBR", "JPN", "USA")


def test_income_group_aggregates_follow_configured_methods(settings, pipeline):
    pipeline("run1")
    pop = {c: live_value(POP, c, 2024) for c in HIC}
    gdp = {c: live_value(GDP, c, 2024) for c in HIC}
    life = {c: live_value(LIFE, c, 2024) for c in HIC}

    assert group(settings, "HIC", POP, 2024)["aggregate_value"] == pytest.approx(sum(pop.values()))
    assert group(settings, "HIC", GDP, 2024)["aggregate_value"] == pytest.approx(sum(gdp.values()))
    gdppc = group(settings, "HIC", GDPPC, 2024)
    assert gdppc["aggregation_method"] == "ratio"
    assert gdppc["aggregate_value"] == pytest.approx(sum(gdp.values()) / sum(pop.values()))
    assert gdppc["simple_mean"] == pytest.approx(sum(live_value(GDPPC, c, 2024) for c in HIC) / 5)
    weighted = sum(life[c] * pop[c] for c in HIC) / sum(pop.values())
    assert group(settings, "HIC", LIFE, 2024)["aggregate_value"] == pytest.approx(weighted)
    assert (gdppc["countries_in_group"], gdppc["is_complete"], gdppc["countries"]) == (
        5,
        True,
        ";".join(HIC),
    )
    assert len(mart(settings, "v_income_group_by_year")) == 3 * 4 * 7  # groups x indicators x years


def test_income_group_with_a_missing_input_is_flagged_incomplete(settings, pipeline, fake_session):
    drop_row(fake_session, POP, "JPN", 2024)
    pipeline("run1")
    rest = [c for c in HIC if c != "JPN"]
    gdppc = group(settings, "HIC", GDPPC, 2024)
    # JPN has GDP per capita but no population, so it cannot enter the ratio at all
    assert gdppc["countries_with_value"] == 5
    assert (gdppc["countries_in_aggregate"], gdppc["is_complete"]) == (4, False)
    assert gdppc["aggregate_value"] == pytest.approx(
        sum(live_value(GDP, c, 2024) for c in rest) / sum(live_value(POP, c, 2024) for c in rest)
    )
    assert group(settings, "HIC", POP, 2024)["is_complete"] is False
    assert group(settings, "HIC", POP, 2023)["is_complete"] is True


def test_default_aggregation_is_simple_mean(settings, ingest):
    ingest("run1")
    life = next(i for i in settings.indicators if i.code == LIFE)
    plain = dataclasses.replace(life, aggregation=Aggregation())
    run_transform(
        dataclasses.replace(
            settings, indicators=tuple(plain if i.code == LIFE else i for i in settings.indicators)
        )
    )
    row = group(settings, "HIC", LIFE, 2024)
    assert row["aggregation_method"] == "mean"
    assert row["aggregate_value"] == pytest.approx(row["simple_mean"])


# ---- extras ------------------------------------------------------------------------------------


def test_source_revisions_are_listed(settings, pipeline, fake_session):
    pipeline("run1")
    assert mart(settings, "v_source_revisions") == []
    set_bra(fake_session, POP, 2020, 1.0)
    pipeline("run2")
    (rev,) = mart(settings, "v_source_revisions")
    assert (rev["country_iso3"], rev["year"], rev["new_value"], rev["detected_in_run_id"]) == (
        "BRA",
        2020,
        1.0,
        "run2",
    )
    assert rev["previous_value"] == live_value(POP, "BRA", 2020)


def test_freshness_reports_the_published_run(settings, pipeline):
    pipeline("run1")
    rows = mart(settings, "v_data_freshness")
    assert {(r["ingest_run_id"], r["values_present"], r["values_expected"]) for r in rows} == {
        ("run1", 63, 63)
    }
    assert {str(r["source_lastupdated"]) for r in rows} == {"2026-10-08"}


def test_base_view_has_readable_names(settings, pipeline):
    pipeline("run1")
    (row,) = mart(
        settings,
        "v_indicator_value",
        f"country_iso3 = 'IND' AND indicator_code = '{POP}' AND year = 2024",
    )
    assert (row["country_name"], row["indicator"], row["income_group_name"], row["has_value"]) == (
        "India", "population", "Lower middle income", True,
    )  # fmt: skip
    assert query(settings, "SELECT count(*) FROM marts.v_indicator_value") == [(252,)]
