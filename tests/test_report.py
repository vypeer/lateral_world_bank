from __future__ import annotations

import csv

import duckdb
import pytest

from wbpipe.quality import DataQualityError, run_checks
from wbpipe.report import EXPORTS, ReportError, run_report


def test_report_requires_checks_for_the_current_transform(settings, pipeline):
    pipeline("run1")
    with pytest.raises(ReportError, match="has not been checked"):
        run_report(settings)
    run_checks(settings)
    pipeline("run2")  # a newer transform invalidates the earlier check
    with pytest.raises(ReportError, match="has not been checked"):
        run_report(settings)


def test_report_refuses_to_publish_after_blocking_failure(settings, pipeline):
    pipeline("run1")
    with duckdb.connect(str(settings.paths.warehouse)) as con:
        con.execute("DELETE FROM core.dim_year WHERE year = 2018")
    with pytest.raises(DataQualityError):
        run_checks(settings)
    with pytest.raises(ReportError, match="fct_dimension_integrity"):
        run_report(settings)


def test_report_exports_every_mart(settings, pipeline):
    pipeline("run1")
    run_checks(settings)
    paths = run_report(settings)
    assert sorted(p.name for p in paths) == sorted(EXPORTS.values())
    with open(settings.paths.output_dir / "q2_yoy_change.csv") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 252
    assert rows[0]["country_iso3"] == "BRA" and rows[0]["yoy_status"] == "prior_out_of_scope"
    with open(settings.paths.output_dir / "q1_missing_values.csv") as fh:
        assert fh.readline().startswith("country_iso3,")  # header even when there are no gaps


def test_unchanged_data_reexports_identical_files(settings, pipeline):
    pipeline("run1")
    run_checks(settings)
    first = {p.name: p.read_bytes() for p in run_report(settings)}
    second = {p.name: p.read_bytes() for p in run_report(settings)}
    assert first == second
