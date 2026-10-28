"""Sanity checks on the captured API fixtures so later tests can rely on their shape."""

from __future__ import annotations

import hashlib

from conftest import LIVE


def test_fixture_files_match_manifest(live_manifest):
    for entry in live_manifest:
        body = (LIVE / entry["file"]).read_bytes()
        assert hashlib.sha256(body).hexdigest() == entry["sha256"], entry["name"]


def test_observations_are_paged_in_two(load_live):
    page1 = load_live("observations_SP.POP.TOTL_page1")
    page2 = load_live("observations_SP.POP.TOTL_page2")
    assert page1[0]["pages"] == page2[0]["pages"] == 2
    assert len(page1[1]) + len(page2[1]) == page1[0]["total"] == 63


def test_error_responses_are_http_200_message_bodies(live_manifest, load_live):
    errors = [e for e in live_manifest if e["name"].startswith("error_")]
    assert len(errors) == 2
    for entry in errors:
        assert entry["http_status"] == 200
        assert "message" in load_live(entry["name"])[0]
