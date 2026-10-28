"""DuckDB warehouse connection shared by all pipeline steps."""

from __future__ import annotations

from pathlib import Path

import duckdb

from wbpipe.config import Settings


def connect(settings: Settings, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the warehouse, creating it and the raw schema on first use."""
    if read_only:
        return duckdb.connect(str(settings.paths.warehouse), read_only=True)
    settings.paths.warehouse.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(settings.paths.warehouse))
    con.execute((settings.paths.sql_dir / "raw" / "schema.sql").read_text())
    return con


def sql_files(settings: Settings, layer: str) -> list[Path]:
    """SQL model files of a layer, in execution order (sorted by their numeric prefix)."""
    return sorted((settings.paths.sql_dir / layer).glob("*.sql"))
