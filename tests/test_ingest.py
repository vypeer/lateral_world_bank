from __future__ import annotations

import hashlib
import json

import pytest
from conftest import BASE_URL, LIVE, FakeResponse, query

from wbpipe.api_client import WorldBankApiError
from wbpipe.ingest import country_request, plan_requests

COUNTRIES = "BRA;CAN;DEU;GBR;IND;JPN;MEX;USA;ZAF"
EXPECTED_PAGES = 1 + 4 * (1 + 2)  # countries + per indicator (metadata + 2 observation pages)


def test_plan_is_config_driven(settings):
    assert country_request(settings).path == f"country/{COUNTRIES}"
    reqs = plan_requests(settings, settings.countries)
    assert [r.endpoint for r in reqs].count("observation") == 4
    assert reqs[1].path == f"country/{COUNTRIES}/indicator/SP.POP.TOTL"
    assert reqs[1].params == {"date": "2018:2024"}


def test_first_run_lands_and_registers_every_page(settings, ingest):
    result = ingest("run1")
    assert (result.n_pages, result.n_new_responses) == (EXPECTED_PAGES, EXPECTED_PAGES)
    assert query(settings, "SELECT status, n_pages FROM raw.ingest_run") == [
        ("succeeded", EXPECTED_PAGES)
    ]
    landed = settings.paths.landing_dir / "run1" / "observation" / "SP.POP.TOTL" / "page_0002.json"
    assert landed.read_bytes() == (LIVE / "observations_SP.POP.TOTL_page2.json").read_bytes()
    assert query(settings, "SELECT DISTINCT source_lastupdated::VARCHAR FROM raw.api_response "
                           "WHERE endpoint = 'observation'") == [("2026-10-08",)]  # fmt: skip


def test_every_response_is_traceable_to_its_landed_file(settings, ingest):
    ingest("run1")
    rows = query(
        settings,
        "SELECT f.landing_path, r.payload_sha256, r.payload::VARCHAR FROM raw.api_fetch f "
        "JOIN raw.api_response r USING (response_id)",
    )
    assert len(rows) == EXPECTED_PAGES
    for landing_path, sha, payload in rows:
        body = (settings.paths.landing_dir / landing_path).read_bytes()
        assert hashlib.sha256(body).hexdigest() == sha
        assert payload.encode() == body  # stored verbatim


def test_rerun_with_unchanged_source_adds_no_responses(settings, ingest):
    ingest("run1")
    result = ingest("run2")
    assert result.n_new_responses == 0
    assert query(settings, "SELECT count(*) FROM raw.api_response") == [(EXPECTED_PAGES,)]
    assert query(settings, "SELECT run_id, count(*) FROM raw.api_fetch GROUP BY 1 ORDER BY 1") == [
        ("run1", EXPECTED_PAGES),
        ("run2", EXPECTED_PAGES),
    ]


def test_rerun_with_a_revised_value_stores_only_the_changed_page(settings, ingest, fake_session):
    ingest("run1")
    meta, rows = json.loads((LIVE / "observations_SP.POP.TOTL_page2.json").read_bytes())
    rows[0]["value"] += 1
    url = f"{BASE_URL}/country/{COUNTRIES}/indicator/SP.POP.TOTL?format=json&date=2018:2024&page=2"
    fake_session.override(url, json.dumps([meta, rows]).encode())
    assert ingest("run2").n_new_responses == 1
    assert query(settings, "SELECT first_run_id, page FROM raw.api_response "
                           "WHERE first_run_id = 'run2'") == [("run2", 2)]  # fmt: skip


def test_failed_run_is_recorded_and_registers_nothing(settings, ingest, fake_session):
    ingest("run1")
    fake_session.queued["SP.DYN.LE00.IN?format"].append(
        FakeResponse(200, (LIVE / "error_invalid_indicator.json").read_bytes())
    )
    with pytest.raises(WorldBankApiError):
        ingest("run2")
    assert query(settings, "SELECT run_id, status, error IS NOT NULL FROM raw.ingest_run "
                           "ORDER BY run_id") == [("run1", "succeeded", False),
                                                  ("run2", "failed", True)]  # fmt: skip
    assert query(settings, "SELECT count(*) FROM raw.api_fetch WHERE run_id = 'run2'") == [(0,)]
    assert query(settings, "SELECT count(*) FROM raw.api_response") == [(EXPECTED_PAGES,)]
