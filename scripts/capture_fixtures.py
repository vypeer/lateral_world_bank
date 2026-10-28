"""Capture real World Bank API responses as test fixtures.

Run manually (it calls the live API); tests only ever read the saved files.

    python scripts/capture_fixtures.py

Each response body is saved byte-for-byte to tests/fixtures/live/<name>.<ext> and
described in tests/fixtures/live/manifest.json (URL, HTTP status, capture time, sha256).
Stdlib only, so it runs before the project's dependencies are installed.

    python scripts/capture_fixtures.py --only income_history_oghist   # one fixture
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://api.worldbank.org/v2"
COUNTRIES = ["BRA", "CAN", "DEU", "GBR", "IND", "JPN", "MEX", "USA", "ZAF"]
INDICATORS = ["SP.POP.TOTL", "NY.GDP.MKTP.CD", "NY.GDP.PCAP.CD", "SP.DYN.LE00.IN"]
DATE_RANGE = "2018:2024"
# 9 countries x 7 years = 63 rows, so per_page=50 forces two pages per indicator.
PER_PAGE = 50

OUT_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "live"
# World Bank historical income classification (not part of API v2; see README). This is the
# link on the "World Bank Country and Lending Groups" help-desk page; it serves the current edition.
OGHIST_URL = "https://ddh-openapi.worldbank.org/resources/DR0095334/download"


def requests_to_capture() -> list[tuple[str, str, str]]:
    """Return (fixture_name, description, url) for every response to capture."""
    countries = ";".join(COUNTRIES)
    reqs: list[tuple[str, str, str]] = [
        (
            "country_metadata",
            "Metadata for all configured countries (one page)",
            f"{BASE_URL}/country/{countries}?format=json&per_page={PER_PAGE}",
        )
    ]
    for code in INDICATORS:
        reqs.append(
            (
                f"indicator_metadata_{code}",
                f"Metadata for indicator {code}",
                f"{BASE_URL}/indicator/{code}?format=json",
            )
        )
        for page in (1, 2):
            reqs.append(
                (
                    f"observations_{code}_page{page}",
                    f"{code} observations, page {page} of 2",
                    f"{BASE_URL}/country/{countries}/indicator/{code}"
                    f"?format=json&date={DATE_RANGE}&per_page={PER_PAGE}&page={page}",
                )
            )
    reqs += [
        (
            "observations_page_beyond_last",
            "Requesting a page past the last one (page=3 of 2)",
            f"{BASE_URL}/country/{countries}/indicator/SP.POP.TOTL"
            f"?format=json&date={DATE_RANGE}&per_page={PER_PAGE}&page=3",
        ),
        (
            "error_invalid_indicator",
            "Unknown indicator code: HTTP 200 with a message body instead of data",
            f"{BASE_URL}/country/BRA/indicator/NOT.A.REAL.CODE?format=json",
        ),
        (
            "error_invalid_country",
            "Unknown country code: HTTP 200 with a message body instead of data",
            f"{BASE_URL}/country/XXX/indicator/SP.POP.TOTL?format=json&date={DATE_RANGE}",
        ),
        (
            "income_history_oghist",
            "Historical income classification workbook (OGHIST.xlsx)",
            OGHIST_URL,
        ),
    ]
    return reqs


def fetch(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "wbpipe-fixture-capture"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read()


def main(argv: list[str]) -> int:
    only = argv[argv.index("--only") + 1] if "--only" in argv else None
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT_DIR / "manifest.json"
    existing = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    manifest = {e["name"]: e for e in existing}
    for name, description, url in requests_to_capture():
        if only and name != only:
            continue
        status, body = fetch(url)
        is_json = url != OGHIST_URL
        if is_json:
            json.loads(body)  # fail loudly if the API returned something that is not JSON
        filename = f"{name}.json" if is_json else f"{name}.xlsx"
        (OUT_DIR / filename).write_bytes(body)
        manifest[name] = {
            "name": name,
            "file": filename,
            "description": description,
            "url": url,
            "http_status": status,
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
        }
        print(f"{status} {name} ({len(body)} bytes)")
        time.sleep(0.3)  # be polite to the public API
    for entry in manifest.values():
        entry.setdefault("file", f"{entry['name']}.json")
    manifest_path.write_text(json.dumps(list(manifest.values()), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
