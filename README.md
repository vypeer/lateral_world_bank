# lateral_world_bank

A local, configuration-driven pipeline that pulls yearly country indicators from the public
[World Bank API v2](https://datahelpdesk.worldbank.org/knowledgebase/articles/889392), models
them in DuckDB and publishes reporting outputs. Analysts can query the results without knowing
the API, and missing data is always explicit.

- **Scope (configurable):** BRA, CAN, DEU, GBR, IND, JPN, MEX, USA, ZAF · population, GDP
  (current US$), GDP per capita (current US$), life expectancy · 2018–2024
- **Stack:** Python 3.10+ · DuckDB (one local file) · plain SQL models · uv · pytest

---

## Quick start

Requirements: [uv](https://docs.astral.sh/uv/) and internet access to `api.worldbank.org`.
No accounts, keys or cloud resources are needed.

```bash
make run
```

This one command installs the locked dependencies into `.venv`, then runs every step:

```
ingest → transform → check → report
```

The results land in `output/` (CSVs and the data-quality report) and in
`data/warehouse.duckdb` (queryable). From a clean checkout it takes a few seconds. The exit code
is non-zero if the API fails after retries or a blocking data-quality check fails.

**Without uv:**

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt   # pinned; also installs this package
wbpipe run
```

**With Docker:**

```bash
docker build -t wbpipe .
docker run --rm -v "$PWD/output:/app/output" wbpipe
```

### Commands

| Command | What it does |
|---|---|
| `make run` / `wbpipe run` | Full pipeline |
| `wbpipe ingest` | Fetch all pages, land them verbatim, register them in `raw` |
| `wbpipe transform [--run-id ID]` | Rebuild staging / core / marts from the latest successful ingest run, or replay an older one |
| `wbpipe check` | Run the data-quality checks; exits 1 on a blocking failure |
| `wbpipe report` | Export the marts to `output/*.csv`. Refuses unless checks passed for the current build |
| `make check` | ruff + mypy (strict) + pytest. Fully offline |
| `make fixtures` | Re-capture the live API responses used as test fixtures (the only live call outside `ingest`) |

### Querying the results

```bash
uv run python -c "import duckdb; print(duckdb.connect('data/warehouse.duckdb', read_only=True).sql('SELECT * FROM marts.v_latest_comparison WHERE indicator = \'gdp_per_capita_usd\''))"
```

Or open `data/warehouse.duckdb` in any DuckDB client and browse the `marts` schema.

---

## How it works

```
World Bank API ──► data/landing/<run_id>/…/page_NNNN.json   (verbatim bytes)
                         │
                   raw.api_response   one row per distinct page (content-addressed)
                   raw.api_fetch      which pages each successful run saw
                   raw.ingest_run     every attempt, incl. failures
                         │  transform (one transaction, pinned to one ingest run)
                   staging.*          typed, flattened rows; each keeps response_id
                         │
                   core.dim_country / dim_indicator / dim_year
                   core.fct_indicator_value          country × indicator × year (+ history)
                         │
                   marts.v_*          answers to the four questions
                         │  check (config/dq_checks.yaml) → quality.dq_result
                         │  report (only if checks passed)
                   output/*.csv, output/dq_report.md
```

```
config/      pipeline.yaml (scope, API, paths, quality settings) · dq_checks.yaml
src/wbpipe/  cli · config · api_client · ingest · transform · quality · report · warehouse
sql/         raw/ · staging/ · core/ · marts/ · quality/   (run in file-name order)
tests/       offline tests; fixtures/live/ holds real captured API responses
docs/        data_model.md: grain, keys, relationships, ERD
output/      sample outputs from a live run (committed)
```

### Data model (summary)

The full description, with a Mermaid ERD, is in [docs/data_model.md](docs/data_model.md).

- **Fact grain:** `core.fct_indicator_value` has one row per configured country × indicator ×
  year. Its business key is `(country_iso3, indicator_code, year)`, and each part is a foreign
  key to `dim_country` (ISO3), `dim_indicator` (WB code) or `dim_year`.
- **Missing is a row, not an absence.** The fact is built from the configured spine.
  `value_status` is one of:
  - `present`
  - `null_in_source`: the API returned the row with a null value
  - `not_returned`: the API omitted the row

  A missing value is never stored as 0.
- **Lineage:** every sourced value carries `response_id`. That leads to the
  `raw.api_response` row, which holds the request URL, the verbatim payload and its sha256,
  and the landing file path.
- **History:** `core.fct_indicator_value_history` (SCD type 2) keeps every version of every
  value. Source corrections are visible and replayable.

### Analytics outputs

| Question | Mart view | Sample output |
|---|---|---|
| Which combinations are missing or incomplete? | `v_missing_values`, `v_series_completeness` | [q1_missing_values.csv](output/q1_missing_values.csv), [q1_series_completeness.csv](output/q1_series_completeness.csv) |
| Year-over-year change, handling missing / zero priors | `v_yoy_change` | [q2_yoy_change.csv](output/q2_yoy_change.csv) |
| Comparison at the latest available year | `v_latest_comparison` | [q3_latest_comparison.csv](output/q3_latest_comparison.csv) |
| Indicators by income group and year | `v_income_group_by_year` | [q4_income_group_by_year.csv](output/q4_income_group_by_year.csv) |
| Extras | `v_indicator_value` (analyst base table), `v_data_freshness`, `v_source_revisions` | [indicator_values.csv](output/indicator_values.csv), [data_freshness.csv](output/data_freshness.csv), [source_revisions.csv](output/source_revisions.csv) |
| Data quality | `quality.dq_result`, `quality.v_gdp_per_capita_consistency` | [dq_report.md](output/dq_report.md), [dq_results.csv](output/dq_results.csv) |

> **About the sample outputs:** at the time of the run (source `lastupdated` 2026-10-08) the
> World Bank had every value for these countries, indicators and years. So the missing-value
> outputs are empty and every series is `complete`. The gap handling (null vs not returned,
> gaps in a series vs trailing gaps, zero or missing priors, incomplete income groups,
> latest-year fallback) is exercised by tests that edit the captured API responses.

### Data quality and tests

- **15 checks** are declared in [config/dq_checks.yaml](config/dq_checks.yaml), each a SQL query
  returning the violating rows.
  - `fail` (blocks publishing): grain, spine completeness, dimension integrity, duplicate or
    unparseable source rows, status semantics, lineage resolving to the checked run, configured
    value ranges, countries missing reference data.
  - `warn` (reported): GDP ÷ population vs GDP per capita outside 1%, missing values,
    out-of-scope rows, stale source, failed ingest since the published run.
  - A check that cannot run is an `error` and always blocks.
- Every execution is appended to `quality.dq_result`.
- **88 tests, fully offline.** API responses come from real captured fixtures
  ([tests/fixtures/live](tests/fixtures/live), with a sha256 manifest) served by a fake HTTP
  session. They cover:
  - paging, retries, `Retry-After` and in-band errors
  - idempotent re-runs: no new raw rows and no rewritten fact rows
  - revisions and replay
  - scope changes and transactional rollback
  - every mart's edge cases
  - each DQ check, with a test showing it catches its defect
  - the publish gate

  CI runs lint, strict mypy and the tests on Python 3.10 and 3.12 (DuckDB 1.x and 2.x).

### Adding a country or indicator

Edit [config/pipeline.yaml](config/pipeline.yaml) and run `make run`. No code changes are needed:

```yaml
countries: [..., FRA]
indicators:
  - code: EN.POP.DNST
    short_name: population_density
    valid_min: 0              # optional plausible range, checked as FAIL
    aggregation: mean         # how income groups combine it: mean | sum | ratio | weighted_mean
```

The request plan, the spine, the dimensions and the marts all derive from configuration.

---

## Key decisions

1. **Land first, then register atomically.** Every page is saved byte-for-byte before
   anything is parsed. A run registers all its pages in one transaction or nothing, so later
   steps never see a partial snapshot. A failed run is recorded with its error.
2. **Content-addressed raw layer.** `response_id = sha256(request_url + payload_sha256)`, so
   re-fetching identical data adds no row. `raw.api_fetch` still records that the run saw it,
   which lets any run's complete snapshot be rebuilt.
3. **Transform is pinned to one ingest run** (default: latest successful). Staging and core
   always describe one consistent snapshot, and `--run-id` replays any older run from `raw`
   without calling the API.
4. **A spine plus `value_status` instead of dropping missing rows.** "Missing" has two different
   source meanings (null vs omitted). Analysts can filter on them without anti-joins.
5. **Idempotent upsert by row hash with SCD2 history.** The hash covers business content only.
   An unchanged re-run writes nothing (tested by comparing the full table). Lineage is refreshed
   without counting as a revision when a page changed because of a different row.
6. **The API client is defensive about the source's quirks:**
   - errors come back as HTTP 200 with a `message` body;
   - `per_page` is a string on some endpoints;
   - an empty result is `[meta, null]`;
   - region names have trailing spaces.

   Pages are cross-checked: `pages`, `total` and `lastupdated` must agree across pages, and the
   row count must equal `total`. A source update during paging fails the run instead of
   mixing two versions.
7. **YoY:** strict previous-year change is primary, and a `yoy_status` explains every NULL. A
   gap-aware, annualised change against the last available year is an extra column. When the
   prior value is 0 the percentage is NULL and the absolute change is kept.
8. **Latest year:** both the latest year *all* countries share (like-for-like ranking) and each
   country's own latest year, labelled where they differ.
9. **Income groups:** each indicator's aggregation is set in config. Population and GDP are
   summed, GDP per capita is ΣGDP/Σpop, and life expectancy is population-weighted. The simple
   mean, country counts and `is_complete` are reported alongside, because a total over part of
   a group isn't comparable.
10. **DQ as data:** checks are YAML + SQL with fail/warn severity, persisted per execution.
    `report` only publishes a build whose latest check passed.

### Assumptions

- **Income group is the current World Bank classification, applied to all years.** The API
  exposes no history (the historical classification is a separate spreadsheet). For example,
  a country that changed group in 2020 appears in its current group for 2018.
- "Latest available year" means the latest year with a non-null value, limited to the
  configured range (2018–2024).
- Values are stored as DOUBLE. Population and GDP are well within exact double precision for
  this use.
- The source's `obs_status`, `unit` and `decimal` are carried through but not interpreted.
- The World Bank's `lastupdated` is per dataset, not per value, so it's used for freshness,
  not for change detection. Changes are detected by content hash.

### Trade-offs

- **DuckDB + plain SQL instead of dbt.** No extra tool to learn or install, and the SQL files
  map one-to-one to dbt models (see the production note). The cost is that dependency order
  comes from file names rather than `ref()`, and the tests are pytest rather than dbt tests.
- **Full refetch of the configured window on every run** rather than incremental API calls.
  It's 9 requests and about 250 rows, and it is the simplest way to catch late revisions to
  past years. Idempotency is handled at the storage layer instead.
- **Dimensions are rebuilt each run (type 1)**; only the fact is versioned. Dimension
  history (e.g. income-group changes) isn't tracked.
- **Landing files are kept for every run**, even when identical to an earlier one. That makes
  each run self-contained on disk at the cost of a few KB of duplicates per run; `raw` itself is
  deduplicated.
- **Outputs include run ids and timestamps** (lineage, freshness), so committed sample files
  change on every run even when values don't.

---

## Production design note

**Scheduling and incremental refreshes**
- Run daily from an orchestrator (Airflow / Dagster / Step Functions) as four tasks mirroring
  the CLI steps, each with retries; the steps are already idempotent.
- The World Bank updates its indicators a few times a year. A cheap first task can fetch page 1
  per indicator and compare `lastupdated` with the last ingested value, skipping the run when
  nothing changed.
- Refresh a rolling window (e.g. the last 10 years) on every run rather than "new years
  only", because past years are revised. Storage stays incremental through hash dedupe in
  `raw` and the row-hash upsert in `core`.

**Late, corrected or changed source data**
- A correction is a content change on an existing key. It closes the old version in
  `fct_indicator_value_history`, opens a new one, updates the current fact, and appears in
  `marts.v_source_revisions`.
- Values that arrive late go from `not_returned` to `present`. Withdrawn values go to
  `null_in_source`. Nothing is overwritten silently, and the raw payload of every version is
  kept.
- Alert when revisions in one run exceed a threshold (e.g. more than 5% of rows); that usually
  means a methodology change upstream.

**Monitoring, alerting and data-quality failures**
- Emit metrics from the run tables: `ingest_run`, `transform_run` and `dq_result` (status,
  pages, new responses, versions opened, check results, retry counts).
- Page on: a failed run, a `fail` / `error` check (which already blocks publishing, so consumers
  keep the last good data), or no successful run within the expected interval (a dead-man's
  switch).
- Send `warn` checks to a daily digest. Keep check history for trend dashboards.
- Freshness SLA per indicator comes from `v_data_freshness`.

**Backfills and replay**
- `raw` is immutable and content-addressed, and the landing bucket is versioned. Any
  historical snapshot can therefore be rebuilt without the API: `transform --run-id <id>`.
  The full history can be rebuilt by replaying runs in order into an empty `core`.
- Widening the scope (more years, countries or indicators) is a config change. The next run
  fetches the wider window and the spine fills in.
- Because writes are idempotent, a backfill can be re-run safely.

**Mapping to Snowflake + AWS Glue / EMR** (Azure equivalents in brackets)

| Local | AWS + Snowflake | Azure |
|---|---|---|
| `data/landing/` | S3 bucket (versioned, partitioned by `run_id`) | ADLS Gen2 |
| `ingest` | Glue Python-shell job or Lambda. Spark/EMR isn't justified at this volume; EMR only at much larger scale | Azure Functions or Data Factory REST copy |
| `raw.*` | Snowpipe / `COPY INTO` from an external stage into a `VARIANT` column; `MERGE` on `response_id` | Same, on Snowflake on Azure, or Synapse / Databricks |
| `staging` / `core` / `marts` SQL | dbt on Snowflake: staging as views, fact as an incremental model (`unique_key` merge), history as a dbt snapshot, marts as (secure) views. `LATERAL FLATTEN` replaces `unnest`; dbt vars replace `getvariable` | dbt on Snowflake / Databricks SQL |
| `dq_checks.yaml` | dbt tests or Soda / Great Expectations, with results in a table; the orchestrator gates publishing | Same |
| Orchestration | MWAA (Airflow) or Step Functions + EventBridge schedule | Data Factory / Synapse pipelines |
| Monitoring | CloudWatch alarms + SNS / Slack | Azure Monitor + Action Groups |
| Infrastructure | Terraform; no secrets needed (public API) | Terraform / Bicep |

---

## AI usage

This project was built with **Claude Code** (Anthropic, Claude Opus 5.5) as a pair programmer,
which the brief encourages.

- **The author's part:** set the scope and working rules; made the design decisions above
  (each open question was put to the author and confirmed before it was built); drove the work
  step by step; reviewed each step before it was committed and pushed.
- **The AI's part:** proposed the design, wrote the code, SQL, tests and documentation,
  captured the API fixtures, and ran the pipeline against the live API to check results.
- **How the output was checked rather than trusted:**
  - every change ran through ruff, strict mypy and the test suite;
  - each commit was re-tested in a clean worktree;
  - `make run` was verified from a fresh clone, through the pip path, and on Python 3.12 with
    DuckDB 2.0;
  - mart numbers were cross-checked against values computed independently in the tests.
- **Bugs this process caught:** a window function evaluated before row filtering in the
  latest-year view, and test helpers that silently discarded earlier fixture edits.

---

## Unfinished work and next steps

- **Dashboard (optional) not built.** The plan was a self-contained static HTML page generated
  from the marts.
- **Docker image not built or run here.** Docker isn't installed on the development machine.
  The same Python 3.12 / DuckDB 2.0 environment it uses was tested with uv.
- **CI not yet confirmed green on GitHub.** The workflow is committed but its runs weren't
  checked from the development environment.
- **Income group history:** load the World Bank's historical classification (OGHIST) as a
  type-2 dimension so groups reflect each year's classification.
- **Skip unchanged runs** using `lastupdated` (see the production note), and add a `--years`
  override for ad-hoc backfills.
- **Wide country-year view** (one column per indicator) for spreadsheet users. It needs a
  config-generated pivot, because DuckDB's dynamic `PIVOT` can't be used in a view.
- **dbt port** of the SQL layer if this moves to a warehouse (see the mapping above).
