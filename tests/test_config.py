from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from wbpipe.config import ConfigError, load_settings, parse_settings

REPO = Path(__file__).resolve().parents[1]
RAW = yaml.safe_load((REPO / "config" / "pipeline.yaml").read_text())


BRIEF = ("BRA", "CAN", "DEU", "GBR", "IND", "JPN", "MEX", "USA", "ZAF")


def test_repo_config_loads():
    settings = load_settings(REPO / "config" / "pipeline.yaml", root=REPO)
    assert settings.all_countries and settings.countries is None
    assert settings.focus_countries == BRIEF
    assert settings.indicators[0].code == "SP.POP.TOTL"
    assert list(settings.years) == list(range(2018, 2025))
    assert settings.paths.warehouse == REPO / "data" / "warehouse.duckdb"


def test_fingerprint_changes_with_config():
    a = parse_settings(RAW, REPO)
    explicit = copy.deepcopy(RAW)
    explicit["countries"] = list(BRIEF)
    wider = copy.deepcopy(explicit)
    wider["countries"].append("FRA")
    assert a.fingerprint() == parse_settings(copy.deepcopy(RAW), REPO).fingerprint()
    fingerprints = {parse_settings(c, REPO).fingerprint() for c in (RAW, explicit, wider)}
    assert len(fingerprints) == 3
    focus_only = copy.deepcopy(RAW)
    focus_only["focus_countries"] = ["FRA"]
    assert parse_settings(focus_only, REPO).fingerprint() == a.fingerprint()  # display-only


def test_explicit_country_list_is_supported():
    raw = copy.deepcopy(RAW)
    raw["countries"] = ["bra", " ind "]
    raw["focus_countries"] = ["IND"]
    settings = parse_settings(raw, REPO)
    assert settings.countries == ("BRA", "IND") and not settings.all_countries
    assert settings.focus_countries == ("IND",)


def test_focus_countries_are_optional():
    raw = copy.deepcopy(RAW)
    del raw["focus_countries"]
    assert parse_settings(raw, REPO).focus_countries == ()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda c: c.update(countries=["BRA", "br"]), "ISO3"),
        (lambda c: c.update(countries=["BRA", "BRA"]), "duplicates"),
        (lambda c: c.update(countries="some"), "'all' or a list"),
        (lambda c: c["indicators"][0].update(short_name="Pop Total"), "snake_case"),
        (lambda c: c["indicators"][0].update(short_name='x"; DROP TABLE t; --'), "snake_case"),
        (lambda c: c.update(countries=[]), "non-empty"),
        (lambda c: c.update(countries=["BRA"]), "not in countries"),  # focus outside scope
        (lambda c: c["focus_countries"].append("xx"), "focus_countries must be ISO3"),
        (lambda c: c["indicators"].append(dict(c["indicators"][0])), "unique"),
        (lambda c: c["indicators"].append({"code": "bad code", "short_name": "x"}), "indicator"),
        (lambda c: c["years"].update(start=2025), "after"),
        (lambda c: c.pop("api"), "missing 'api'"),
        (lambda c: c["indicators"][3].update(valid_min=101), "valid_min is above valid_max"),
        (lambda c: c["indicators"].pop(0), "unconfigured indicators"),  # used by gdp_per_capita
        (lambda c: c["indicators"][0].update(aggregation="median"), "method must be one of"),
        (
            lambda c: c["indicators"][2].update(aggregation={"method": "ratio", "numerator": "X"}),
            "takes exactly",
        ),
        (
            lambda c: c["indicators"][3]["aggregation"].update(weight="NOT.CONFIGURED"),
            "aggregation uses unconfigured",
        ),
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


def test_aggregation_defaults_to_mean_and_accepts_shorthand():
    raw = copy.deepcopy(RAW)
    del raw["indicators"][3]["aggregation"]
    settings = parse_settings(raw, REPO)
    assert settings.indicators[0].aggregation.method == "sum"  # 'sum' shorthand
    assert settings.indicators[3].aggregation.method == "mean"
    assert settings.indicators[2].aggregation.denominator == "SP.POP.TOTL"
