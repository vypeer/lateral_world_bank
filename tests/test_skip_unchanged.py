"""Ingest skips the full fetch when cheap probes show the source unchanged."""

from __future__ import annotations

import dataclasses
import json

import pytest
from conftest import BASE_URL, COUNTRIES, LIVE, edit_observations, query

from wbpipe.ingest import run_ingest


@pytest.fixture
def skipping(settings):
    return dataclasses.replace(settings, skip_if_unchanged=True)


@pytest.fixture
def ingest_run(fake_session):
    from wbpipe.api_client import WorldBankClient

    def _run(settings, run_id, **kwargs):
        client = WorldBankClient(settings.api, session=fake_session, sleep=lambda _: None)
        return run_ingest(settings, client=client, run_id=run_id, **kwargs)

    return _run


def test_unchanged_source_is_skipped(skipping, ingest_run, fake_session):
    first = ingest_run(skipping, "run1")
    assert not first.skipped
    fake_session.calls.clear()
    second = ingest_run(skipping, "run2")
    assert (second.skipped, second.run_id, second.n_pages) == (True, "run1", 0)
    # one country page + one per_page=1 probe per indicator; nothing landed or registered
    assert len(fake_session.calls) == 1 + 4
    assert all("per_page=1&" in u for u in fake_session.calls if "/indicator/" in u)
    assert not (skipping.paths.landing_dir / "run2").exists()
    assert query(skipping, "SELECT run_id FROM raw.ingest_run") == [("run1",)]
    assert query(skipping, "SELECT unchanged_since_run_id FROM raw.ingest_skip") == [("run1",)]


def test_force_always_fetches(skipping, ingest_run):
    ingest_run(skipping, "run1")
    result = ingest_run(skipping, "run2", force=True)
    assert not result.skipped and result.n_pages == 13 and result.n_new_responses == 0


def test_new_source_release_triggers_a_full_run(skipping, ingest_run, fake_session):
    ingest_run(skipping, "run1")
    for page in (1, 2):  # a WDI release bumps lastupdated on every page
        edit_observations(
            fake_session, "NY.GDP.MKTP.CD",
            lambda meta, rows: meta.update(lastupdated="2026-12-15"), page=page,
        )  # fmt: skip
    result = ingest_run(skipping, "run2")
    assert not result.skipped and result.n_new_responses == 2


def test_country_metadata_change_triggers_a_full_run(skipping, ingest_run, fake_session):
    ingest_run(skipping, "run1")
    meta, rows = json.loads((LIVE / "country_metadata.json").read_bytes())
    rows[0]["incomeLevel"] = {"id": "HIC", "iso2code": "XD", "value": "High income"}
    fake_session.override(
        f"{BASE_URL}/country/{COUNTRIES}?format=json", json.dumps([meta, rows]).encode()
    )
    assert not ingest_run(skipping, "run2").skipped


def test_scope_change_triggers_a_full_run(skipping, ingest_run):
    ingest_run(skipping, "run1")
    narrower = dataclasses.replace(skipping, indicators=skipping.indicators[:2])
    assert not ingest_run(narrower, "run2").skipped


def test_no_previous_run_means_a_full_run(skipping, ingest_run):
    assert not ingest_run(skipping, "run1").skipped
