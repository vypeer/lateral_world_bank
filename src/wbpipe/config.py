"""Load and validate config/pipeline.yaml into typed settings."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_ISO3 = re.compile(r"^[A-Z]{3}$")
_INDICATOR = re.compile(r"^[A-Z0-9]+(\.[A-Z0-9]+)+$")


class ConfigError(ValueError):
    """Raised when the pipeline configuration is invalid."""


@dataclass(frozen=True)
class ApiSettings:
    base_url: str
    per_page: int
    timeout_seconds: float
    max_retries: int
    backoff_seconds: float


@dataclass(frozen=True)
class Indicator:
    code: str
    short_name: str
    # Plausible value range (inclusive) checked by data quality; None = unbounded.
    valid_min: float | None = None
    valid_max: float | None = None


@dataclass(frozen=True)
class Paths:
    landing_dir: Path
    warehouse: Path
    sql_dir: Path
    output_dir: Path


@dataclass(frozen=True)
class GdpPerCapitaRule:
    """GDP / population should equal GDP per capita within a relative tolerance."""

    gdp: str
    population: str
    gdp_per_capita: str
    tolerance: float


@dataclass(frozen=True)
class QualitySettings:
    checks_file: Path
    gdp_per_capita: GdpPerCapitaRule | None
    max_source_age_days: int


@dataclass(frozen=True)
class Settings:
    api: ApiSettings
    start_year: int
    end_year: int
    countries: tuple[str, ...]
    indicators: tuple[Indicator, ...]
    paths: Paths
    quality: QualitySettings

    @property
    def years(self) -> range:
        return range(self.start_year, self.end_year + 1)

    def fingerprint(self) -> str:
        """Stable hash of the requested data scope, recorded on every ingest run.

        Only settings that change what is fetched count; quality thresholds do not.
        """
        scope = {
            "base_url": self.api.base_url,
            "countries": self.countries,
            "indicators": [i.code for i in self.indicators],
            "years": [self.start_year, self.end_year],
        }
        return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"missing '{key}' in {where}")
    return mapping[key]


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def parse_settings(raw: dict[str, Any], root: Path) -> Settings:
    """Validate a parsed YAML document. Relative paths are resolved against ``root``."""
    api_raw = _require(raw, "api", "config")
    api = ApiSettings(
        base_url=str(_require(api_raw, "base_url", "api")).rstrip("/"),
        per_page=int(_require(api_raw, "per_page", "api")),
        timeout_seconds=float(api_raw.get("timeout_seconds", 30)),
        max_retries=int(api_raw.get("max_retries", 5)),
        backoff_seconds=float(api_raw.get("backoff_seconds", 1.0)),
    )
    if api.per_page < 1 or api.max_retries < 0 or api.backoff_seconds < 0:
        raise ConfigError("api.per_page must be >= 1; max_retries and backoff_seconds >= 0")

    years = _require(raw, "years", "config")
    start, end = int(_require(years, "start", "years")), int(_require(years, "end", "years"))
    if start > end:
        raise ConfigError(f"years.start ({start}) is after years.end ({end})")

    countries = tuple(str(c).strip().upper() for c in _require(raw, "countries", "config"))
    bad = [c for c in countries if not _ISO3.match(c)]
    if bad:
        raise ConfigError(f"countries must be ISO3 codes, got {bad}")
    if not countries or len(set(countries)) != len(countries):
        raise ConfigError("countries must be a non-empty list without duplicates")

    indicators = tuple(
        Indicator(
            code=str(_require(i, "code", "indicator")).strip(),
            short_name=str(_require(i, "short_name", "indicator")).strip(),
            valid_min=_optional_float(i.get("valid_min")),
            valid_max=_optional_float(i.get("valid_max")),
        )
        for i in _require(raw, "indicators", "config")
    )
    bad = [i.code for i in indicators if not _INDICATOR.match(i.code)]
    if bad:
        raise ConfigError(f"invalid indicator codes {bad}")
    for attr in ("code", "short_name"):
        values = [getattr(i, attr) for i in indicators]
        if not values or len(set(values)) != len(values):
            raise ConfigError(f"indicator {attr}s must be non-empty and unique")
    for i in indicators:
        if i.valid_min is not None and i.valid_max is not None and i.valid_min > i.valid_max:
            raise ConfigError(f"indicator {i.code}: valid_min is above valid_max")

    paths_raw = _require(raw, "paths", "config")
    paths = Paths(
        **{
            key: (root / str(_require(paths_raw, key, "paths"))).resolve()
            for key in ("landing_dir", "warehouse", "sql_dir", "output_dir")
        }
    )

    quality_raw = raw.get("quality") or {}
    rule_raw = quality_raw.get("gdp_per_capita")
    rule = None
    if rule_raw:
        rule = GdpPerCapitaRule(
            gdp=str(_require(rule_raw, "gdp", "quality.gdp_per_capita")),
            population=str(_require(rule_raw, "population", "quality.gdp_per_capita")),
            gdp_per_capita=str(_require(rule_raw, "gdp_per_capita", "quality.gdp_per_capita")),
            tolerance=float(rule_raw.get("tolerance", 0.01)),
        )
        unknown = {rule.gdp, rule.population, rule.gdp_per_capita} - {i.code for i in indicators}
        if unknown:
            raise ConfigError(f"quality.gdp_per_capita uses unconfigured indicators {unknown}")
    quality = QualitySettings(
        checks_file=(root / str(quality_raw.get("checks_file", "config/dq_checks.yaml"))).resolve(),
        gdp_per_capita=rule,
        max_source_age_days=int(quality_raw.get("max_source_age_days", 400)),
    )
    return Settings(
        api=api,
        start_year=start,
        end_year=end,
        countries=countries,
        indicators=indicators,
        paths=paths,
        quality=quality,
    )


def load_settings(path: str | Path, root: Path | None = None) -> Settings:
    """Load settings from YAML. Paths resolve against ``root`` (default: current directory)."""
    with open(path) as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return parse_settings(raw, root or Path.cwd())
