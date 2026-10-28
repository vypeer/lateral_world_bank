from __future__ import annotations

import json

import pytest
import requests
from conftest import BASE_URL, LIVE, FakeResponse

from wbpipe.api_client import PagingError, RetriesExhausted, WorldBankApiError, WorldBankClient

COUNTRIES = "BRA;CAN;DEU;GBR;IND;JPN;MEX;USA;ZAF"
POP_PATH = f"country/{COUNTRIES}/indicator/SP.POP.TOTL"
DATES = {"date": "2018:2024"}


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(settings, fake_session, sleeps) -> WorldBankClient:
    return WorldBankClient(settings.api, session=fake_session, sleep=sleeps.append)


def page_url(page: int) -> str:
    return f"{BASE_URL}/{POP_PATH}?format=json&date=2018:2024&per_page=50&page={page}"


def test_reads_all_pages(client, fake_session):
    pages = client.fetch_all(POP_PATH, DATES)
    assert [p.page for p in pages] == [1, 2]
    assert sum(len(p.rows) for p in pages) == 63
    assert pages[0].body == (LIVE / "observations_SP.POP.TOTL_page1.json").read_bytes()
    assert fake_session.calls == [page_url(1), page_url(2)]  # ';' and ':' kept literal


def test_single_page_metadata(client):
    (page,) = client.fetch_all(f"country/{COUNTRIES}")
    assert len(page.rows) == 9
    assert page.meta["per_page"] == "50"  # metadata endpoints return per_page as a string


def test_in_band_error_is_raised_without_retry(client, fake_session, sleeps):
    fake_session.queued["NOT.A.REAL.CODE"].append(
        FakeResponse(200, (LIVE / "error_invalid_indicator.json").read_bytes())
    )
    with pytest.raises(WorldBankApiError, match="120 Invalid value"):
        client.fetch_all("country/BRA/indicator/NOT.A.REAL.CODE")
    assert sleeps == []


def test_transient_failures_are_retried(client, fake_session, sleeps):
    fake_session.queued["page=2"] += [
        FakeResponse(503, b"busy", {"Retry-After": "7"}),
        requests.ConnectionError("reset"),
    ]
    pages = client.fetch_all(POP_PATH, DATES)
    assert sum(len(p.rows) for p in pages) == 63
    assert sleeps == [7.0, 0.0]  # Retry-After honoured, then backoff (0 in tests)


def test_html_with_200_is_treated_as_transient(client, fake_session):
    fake_session.queued["page=1"].append(FakeResponse(200, b"<html>gateway</html>"))
    assert len(client.fetch_all(POP_PATH, DATES)) == 2


def test_gives_up_after_max_retries(client, fake_session, sleeps):
    fake_session.queued["page=1"] += [requests.Timeout("slow")] * 3
    with pytest.raises(RetriesExhausted, match="3 attempts"):
        client.fetch_all(POP_PATH, DATES)
    assert len(sleeps) == 2


def test_client_errors_are_not_retried(client, fake_session, sleeps):
    fake_session.queued["page=1"].append(FakeResponse(400, b"bad request"))
    with pytest.raises(WorldBankApiError, match="HTTP 400"):
        client.fetch_all(POP_PATH, DATES)
    assert sleeps == []


def test_missing_rows_across_pages_are_detected(client, fake_session):
    meta, rows = json.loads((LIVE / "observations_SP.POP.TOTL_page2.json").read_bytes())
    fake_session.override(page_url(2), json.dumps([meta, rows[:-1]]).encode())
    with pytest.raises(PagingError, match="received 62 rows"):
        client.fetch_all(POP_PATH, DATES)


def test_source_update_between_pages_is_detected(client, fake_session):
    meta, rows = json.loads((LIVE / "observations_SP.POP.TOTL_page2.json").read_bytes())
    fake_session.override(
        page_url(2), json.dumps([{**meta, "lastupdated": "2099-01-01"}, rows]).encode()
    )
    with pytest.raises(PagingError, match="lastupdated changed"):
        client.fetch_all(POP_PATH, DATES)


def test_empty_result_set(client, fake_session):
    body = json.dumps([{"page": 1, "pages": 1, "per_page": 50, "total": 0}, None]).encode()
    fake_session.override(f"{BASE_URL}/{POP_PATH}?format=json&date=2030:2030", body)
    (page,) = client.fetch_all(POP_PATH, {"date": "2030:2030"})
    assert page.rows == []


def test_unexpected_shape_is_an_error(client, fake_session):
    fake_session.queued["page=1"].append(FakeResponse(200, b'{"not": "a list"}'))
    with pytest.raises(WorldBankApiError, match="unexpected response shape"):
        client.fetch_all(POP_PATH, DATES)
