"""Load and validate config/pipeline.yaml into typed settings."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
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


@dataclass(frozen=True)
class Paths:
    landing_dir: Path
    warehouse: Path
    sql_dir: Path
    output_dir: Path


@dataclass(frozen=True)
class Settings:
    api: ApiSettings
    start_year: int
    end_year: int
    countries: tuple[str, ...]
    indicators: tuple[Indicator, ...]
    paths: Paths
    gdp_per_capita_tolerance: float

    @property
    def years(self) -> range:
        return range(self.start_year, self.end_year + 1)

    def fingerprint(self) -> str:
        """Stable hash of the effective config, recorded on every ingest run."""
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"missing '{key}' in {where}")
    return mapping[key]


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

    paths_raw = _require(raw, "paths", "config")
    paths = Paths(
        **{
            key: (root / str(_require(paths_raw, key, "paths"))).resolve()
            for key in ("landing_dir", "warehouse", "sql_dir", "output_dir")
        }
    )

    quality = raw.get("quality", {})
    return Settings(
        api=api,
        start_year=start,
        end_year=end,
        countries=countries,
        indicators=indicators,
        paths=paths,
        gdp_per_capita_tolerance=float(quality.get("gdp_per_capita_tolerance", 0.01)),
    )


def load_settings(path: str | Path, root: Path | None = None) -> Settings:
    """Load settings from YAML. Paths resolve against ``root`` (default: current directory)."""
    with open(path) as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return parse_settings(raw, root or Path.cwd())
