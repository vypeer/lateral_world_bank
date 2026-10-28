"""'all' mode: the scope is every economy the API lists, aggregates excluded."""

from __future__ import annotations

import dataclasses
import json

import pytest
from conftest import BASE_URL, LIVE, query

from wbpipe.quality import DataQualityError, run_checks
from wbpipe.transform import run_transform

AGGREGATES = [
    {"id": "WLD", "iso2Code": "1W", "name": "World",
     "region": {"id": "NA", "iso2code": "NA", "value": "Aggregates"}},
    {"id": "HIC", "iso2Code": "XD", "name": "High income",
     "region": {"id": "NA", "iso2code": "NA", "value": "Aggregates"}},
]  # fmt: skip


@pytest.fixture
def all_mode(settings, fake_session):
    """Settings in 'all' mode, with the API's full country list served as fixture."""
    meta, economies = json.loads((LIVE / "country_metadata.json").read_bytes())
    rows = [AGGREGATES[0], *economies[:4], AGGREGATES[1], *economies[4:]]
    meta = {**meta, "total": len(rows), "per_page": "50"}
    fake_session.override(f"{BASE_URL}/country?format=json", json.dumps([meta, rows]).encode())
    return dataclasses.replace(settings, countries=None)


def test_all_mode_requests_every_economy_and_skips_aggregates(all_mode, ingest_with, fake_session):
    ingest_with(all_mode, "run1")
    observation_calls = [u for u in fake_session.calls if "/indicator/SP.POP.TOTL?" in u]
    assert observation_calls and all("WLD" not in u and "HIC;" not in u for u in observation_calls)
    run_transform(all_mode)
    assert query(all_mode, "SELECT count(*) FROM core.dim_country") == [(9,)]
    assert query(all_mode, "SELECT count(*) FROM core.fct_indicator_value") == [(9 * 4 * 7,)]
    run_checks(all_mode)  # passes: aggregates never enter the scope


def test_focus_countries_come_first_and_are_flagged(all_mode, ingest_with):
    focused = dataclasses.replace(all_mode, focus_countries=("ZAF", "IND"))
    ingest_with(focused, "run1")
    run_transform(focused)
    rows = query(
        focused, "SELECT country_iso3, is_focus FROM core.dim_country ORDER BY config_order"
    )
    assert rows[:2] == [("ZAF", True), ("IND", True)]
    assert {r[1] for r in rows[2:]} == {False}


def test_focus_country_outside_scope_fails_checks(all_mode, ingest_with):
    bad = dataclasses.replace(all_mode, focus_countries=("BRA", "FRA"))  # FRA not listed
    ingest_with(bad, "run1")
    run_transform(bad)
    with pytest.raises(DataQualityError, match="focus_countries_in_scope"):
        run_checks(bad)
