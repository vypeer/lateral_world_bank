"""Run data-quality checks and persist results to quality.dq_result.

Checks are declared in YAML (config/dq_checks.yaml) as SQL returning violating rows. Every
execution is appended to ``quality.dq_result`` and written to ``output/`` as CSV and Markdown,
including when a check fails, so a failed run is still explained.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import yaml

from wbpipe.config import Settings
from wbpipe.warehouse import connect, sql_files

log = logging.getLogger(__name__)

SEVERITIES = ("fail", "warn")
SAMPLE_ROWS = 5


class DataQualityError(RuntimeError):
    """At least one check with severity 'fail' did not pass (or could not run)."""


@dataclass(frozen=True)
class Check:
    name: str
    severity: str
    description: str
    sql: str


@dataclass(frozen=True)
class CheckResult:
    check: Check
    status: str  # pass | warn | fail | error
    violation_count: int | None
    sample: list[dict[str, Any]]
    error: str | None = None

    @property
    def blocking(self) -> bool:
        return self.status in ("fail", "error")


def load_checks(path: Path) -> list[Check]:
    raw = yaml.safe_load(path.read_text()) or {}
    checks = []
    for item in raw.get("checks", []):
        missing = {"name", "severity", "description", "sql"} - set(item)
        if missing:
            raise ValueError(
                f"{path}: check {item.get('name', '?')!r} is missing {sorted(missing)}"
            )
        if item["severity"] not in SEVERITIES:
            raise ValueError(f"{path}: check {item['name']!r} has severity {item['severity']!r}")
        sql = str(item["sql"]).strip().rstrip(";")
        checks.append(
            Check(item["name"], item["severity"], " ".join(item["description"].split()), sql)
        )
    names = [c.name for c in checks]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"{path}: duplicate check names {duplicates}")
    if not checks:
        raise ValueError(f"{path}: no checks defined")
    return checks


def evaluate(con: duckdb.DuckDBPyConnection, check: Check) -> CheckResult:
    try:
        row = con.execute(f"SELECT count(*) FROM ({check.sql})").fetchone()
        count = int(row[0]) if row else 0
        sample: list[dict[str, Any]] = []
        if count:
            payload = con.execute(
                f"SELECT to_json(list(t)) FROM (SELECT * FROM ({check.sql}) LIMIT {SAMPLE_ROWS}) t"
            ).fetchone()
            sample = json.loads(payload[0]) if payload and payload[0] else []
    except duckdb.Error as exc:
        return CheckResult(check, "error", None, [], f"{type(exc).__name__}: {exc}")
    status = "pass" if count == 0 else check.severity
    return CheckResult(check, status, count, sample)


def checked_ingest_run(con: duckdb.DuckDBPyConnection) -> str:
    try:
        row = con.execute(
            "SELECT ingest_run_id FROM core.transform_run ORDER BY transformed_at DESC LIMIT 1"
        ).fetchone()
    except duckdb.CatalogException:
        row = None
    if row is None:
        raise DataQualityError("nothing to check: run `wbpipe transform` first")
    return str(row[0])


def run_checks(settings: Settings) -> list[CheckResult]:
    """Run all checks against the current core layer; raise if any blocking check failed."""
    checks = load_checks(settings.quality.checks_file)
    checked_at = datetime.now(timezone.utc)
    with connect(settings) as con:
        ingest_run_id = checked_ingest_run(con)
        con.execute("SET VARIABLE ingest_run_id = ?", [ingest_run_id])
        con.execute("SET VARIABLE max_source_age_days = ?", [settings.quality.max_source_age_days])
        for path in sql_files(settings, "quality"):
            con.execute(path.read_text())

        results = [evaluate(con, check) for check in checks]
        con.executemany(
            "INSERT INTO quality.dq_result VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                [
                    checked_at, ingest_run_id, r.check.name, r.check.severity, r.status,
                    r.violation_count, json.dumps(r.sample) if r.sample else None,
                    r.check.description, r.error,
                ]
                for r in results
            ],
        )  # fmt: skip

    for r in results:
        level = logging.INFO if r.status == "pass" else logging.WARNING
        log.log(level, "%-32s %-5s %s", r.check.name, r.status.upper(),
                r.error or f"{r.violation_count} violation(s)")  # fmt: skip
    write_outputs(settings.paths.output_dir, results, ingest_run_id, checked_at)

    blocking = [r.check.name for r in results if r.blocking]
    if blocking:
        raise DataQualityError(f"blocking data-quality checks failed: {', '.join(blocking)}")
    return results


def write_outputs(
    output_dir: Path, results: list[CheckResult], ingest_run_id: str, checked_at: datetime
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(output_dir / "dq_results.csv", "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["check_name", "severity", "status", "violation_count", "description", "error"]
        )
        for r in results:
            writer.writerow(
                [r.check.name, r.check.severity, r.status, r.violation_count,
                 r.check.description, r.error or ""]
            )  # fmt: skip
    (output_dir / "dq_report.md").write_text(render_report(results, ingest_run_id, checked_at))


def render_report(results: list[CheckResult], ingest_run_id: str, checked_at: datetime) -> str:
    counts = {s: sum(r.status == s for r in results) for s in ("pass", "warn", "fail", "error")}
    lines = [
        "# Data-quality report",
        "",
        f"- Ingest run checked: `{ingest_run_id}`",
        f"- Checked at: {checked_at:%Y-%m-%d %H:%M:%S} UTC",
        "- Result: " + ", ".join(f"{n} {s}" for s, n in counts.items()),
        "",
        "| Check | Severity | Status | Violations | Description |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        violations = "n/a" if r.violation_count is None else str(r.violation_count)
        lines.append(
            f"| `{r.check.name}` | {r.check.severity} | **{r.status.upper()}** | {violations} "
            f"| {r.check.description} |"
        )
    details = [r for r in results if r.status != "pass"]
    if details:
        lines += ["", "## Details", ""]
        for r in details:
            lines.append(f"### `{r.check.name}` ({r.status})")
            lines.append("")
            if r.error:
                lines += ["```", r.error, "```", ""]
            else:
                shown = f"first {len(r.sample)} of {r.violation_count}"
                lines += [f"Sample ({shown}):", "", "```json"]
                lines += [json.dumps(row, default=str) for row in r.sample]
                lines += ["```", ""]
    return "\n".join(lines).rstrip() + "\n"
