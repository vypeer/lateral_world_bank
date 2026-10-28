from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from wbpipe.config import ConfigError, load_settings, parse_settings

REPO = Path(__file__).resolve().parents[1]
RAW = yaml.safe_load((REPO / "config" / "pipeline.yaml").read_text())


def test_repo_config_loads():
    settings = load_settings(REPO / "config" / "pipeline.yaml", root=REPO)
    assert len(settings.countries) == 9
    assert settings.indicators[0].code == "SP.POP.TOTL"
    assert list(settings.years) == list(range(2018, 2025))
    assert settings.paths.warehouse == REPO / "data" / "warehouse.duckdb"


def test_fingerprint_changes_with_config():
    a = parse_settings(RAW, REPO)
    changed = copy.deepcopy(RAW)
    changed["countries"].append("FRA")
    assert a.fingerprint() == parse_settings(copy.deepcopy(RAW), REPO).fingerprint()
    assert a.fingerprint() != parse_settings(changed, REPO).fingerprint()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda c: c["countries"].append("br"), "ISO3"),
        (lambda c: c["countries"].append("BRA"), "duplicates"),
        (lambda c: c["indicators"].append(dict(c["indicators"][0])), "unique"),
        (lambda c: c["indicators"].append({"code": "bad code", "short_name": "x"}), "indicator"),
        (lambda c: c["years"].update(start=2025), "after"),
        (lambda c: c.pop("api"), "missing 'api'"),
        (lambda c: c["indicators"][3].update(valid_min=101), "valid_min is above valid_max"),
        (lambda c: c["indicators"].pop(0), "unconfigured indicators"),  # used by gdp_per_capita
    ],
)
def test_invalid_config_is_rejected(mutate, message):
    raw = copy.deepcopy(RAW)
    mutate(raw)
    with pytest.raises(ConfigError, match=message):
        parse_settings(raw, REPO)


def test_quality_settings_do_not_change_the_scope_fingerprint():
    changed = copy.deepcopy(RAW)
    changed["quality"]["max_source_age_days"] = 1
    changed["indicators"][0]["valid_min"] = 0
    assert parse_settings(changed, REPO).fingerprint() == parse_settings(RAW, REPO).fingerprint()


def test_gdp_per_capita_rule_is_optional():
    raw = copy.deepcopy(RAW)
    del raw["quality"]["gdp_per_capita"]
    assert parse_settings(raw, REPO).quality.gdp_per_capita is None
