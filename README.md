# lateral_world_bank

A local, configuration-driven pipeline that pulls yearly country indicators from the public
[World Bank API v2](https://datahelpdesk.worldbank.org/knowledgebase/articles/889392), models
them in DuckDB and publishes reporting outputs. Analysts can query the results without knowing
the API, and missing data is always explicit.

- **Scope (configurable):** every economy the API lists (217 today; regional and income
  aggregates excluded), with the brief's countries (BRA, CAN, DEU, GBR, IND, JPN, MEX, USA, ZAF)
  as *focus countries* that the dashboards show by default · population, GDP (current US$),
  GDP per capita (current US$), life expectancy · 2018–2024
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
ingest → transform → check → report → dashboard
```

The results land in `output/` (CSVs, the data-quality report and
[`dashboard.html`](output/dashboard.html)) and in `data/warehouse.duckdb` (queryable). From a clean checkout it takes a few seconds. The exit code
is non-zero if the API fails after retries or a blocking data-quality check fails.

**Live dashboard app** (filters, drill-down, lineage, SQL):

```bash
make app        # builds the warehouse if needed, then serves http://localhost:8501
```

**Without uv:**

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt   # pinned; also installs this package
wbpipe run
pip install -r requirements-app.txt && streamlit run src/wbpipe/app.py   # optional live app
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
| `wbpipe ingest [--force]` | Fetch all pages, land them verbatim, register them in `raw`. Skipped when cheap probes show the source unchanged since the last run; `--force` fetches anyway (also works with `run`) |
| `wbpipe transform [--run-id ID]` | Rebuild staging / core / marts from the latest successful ingest run, or replay an older one |
| `wbpipe check` | Run the data-quality checks; exits 1 on a blocking failure |
| `wbpipe report` | Export the marts to `output/*.csv`. Refuses unless checks passed for the current build |
| `wbpipe dashboard` | Render `output/dashboard.html` from the marts (same publish gate) |
| `make app` | Live dashboard app on http://localhost:8501 (see below) |
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
                   output/*.csv, output/dq_report.md, output/dashboard.html
```

```
config/      pipeline.yaml (scope, API, paths, quality settings) · dq_checks.yaml
src/wbpipe/  cli · config · api_client · ingest · transform · quality · report · dashboard · warehouse
             app · app_data   (optional live dashboard app)
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
| Wide view | `v_country_year_wide`: one row per country-year, one column per indicator (generated from config) | [country_year_wide.csv](output/country_year_wide.csv) |
| Extras | `v_indicator_value` (analyst base table), `v_data_freshness`, `v_source_revisions` | [indicator_values.csv](output/indicator_values.csv), [data_freshness.csv](output/data_freshness.csv), [source_revisions.csv](output/source_revisions.csv) |
| Data quality | `quality.dq_result`, `quality.v_gdp_per_capita_consistency` | [dq_report.md](output/dq_report.md), [dq_results.csv](output/dq_results.csv) |
| All of the above, visually | n/a | [dashboard.html](output/dashboard.html): open it in a browser after `make run` |

### Dashboard

`output/dashboard.html` is a single self-contained file: no external requests, no JavaScript
libraries, charts drawn as inline SVG by `wbpipe dashboard`. It shows:
- coverage and data-quality status tiles;
- income-group trends: one small chart per indicator, three lines;
- a ranked comparison at the latest common year;
- year-over-year heatmaps, country × year;
- a completeness matrix and the missing-value list;
- the check results.

Design choices:
- **Colour encodes entities only where there are few of them.** The 9 countries are never
  9 colours: lines show the 3 income groups, and country comparisons use single-colour bars
  and heatmaps.
- **Light and dark themes are both chosen deliberately**, each with its own colour steps.
- **The diverging heatmap scale was validated** for step separation, contrast against the
  background and colour-blind separation of its two poles.
- **Every chart has hover/keyboard tooltips and a data table**, so nothing depends on colour
  or hovering alone.
- **Source text is HTML-escaped**, so names from the API can't inject markup.

> **About the sample outputs** (source `lastupdated` 2026-10-08, all 217 economies):
> - **Missing data:** the brief's 9 countries are complete, but across all economies 128 GDP and
>   GDP-per-capita values are null in the source. They're listed in `q1_missing_values.csv`:
>   70 belong to series with no data at all, 54 are trailing gaps (not yet published) and 4 are
>   gaps inside a series. Several income-group aggregates are therefore flagged
>   `is_complete = false`.
> - **Data quality:** the run passes with 2 warnings. One is the missing values. The other is
>   that GDP ÷ population differs from the published GDP per capita by more than 1% for 5
>   economies (Cyprus about 30%, Ukraine about 6%, Tanzania, Russia, Morocco), a known
>   difference in the population base the World Bank uses for some per-capita figures.
> - **Tests:** zero or missing priors, values not returned at all, and revisions don't occur in
>   this snapshot. Tests exercise them by editing the captured API responses.

### Live dashboard app

`make app` starts a local [Streamlit](https://streamlit.io) app that queries
`data/warehouse.duckdb` live. It's an optional extra: the pipeline itself doesn't need it.

- **Filters** in one row above the content scope every view: indicator, countries, year range,
  income groups.
- **Tabs:**
  - **Trends:** small multiples per country, with raw values or indexed to the first year,
    and a shared or independent axis.
  - **Compare:** ranked bars at the latest year all *selected* countries share (recomputed
    for the selection), or any chosen year.
  - **Year-over-year:** heatmap, switchable between strict and annualised gap-aware change.
  - **Income groups:** configured aggregate vs simple mean.
  - **Data quality & lineage:** check results with sample rows, completeness, missing values,
    source revisions, run history. You can also trace any value to its request URL, landing
    file and the exact JSON row it came from.
  - **Explore & SQL:** the filtered data long or wide (one column per indicator) with CSV
    download, plus a SQL box over the whole warehouse.
- **Refresh data** runs the full pipeline from the app. Unchanged source data creates no new
  records; changed data appears as revisions.
- **Safe by construction:**
  - every query uses a short-lived **read-only** connection, so the pipeline can rebuild the
    warehouse while the app is open, and the SQL box can't modify anything (results are
    capped at 5,000 rows);
  - filter values are bound parameters, never interpolated into SQL;
  - Streamlit usage telemetry is switched off in `.streamlit/config.toml`.
- **Charts reuse the validated palette.** The app uses a dark theme. Chart colours are fixed
  values validated against each background, so they follow the theme set in
  `.streamlit/config.toml` (`base = "light"` switches both).

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
- **113 tests, fully offline.** API responses come from real captured fixtures
  ([tests/fixtures/live](tests/fixtures/live), with a sha256 manifest) served by a fake HTTP
  session. They cover:
  - paging, retries, `Retry-After` and in-band errors
  - idempotent re-runs: no new raw rows and no rewritten fact rows
  - revisions and replay
  - scope changes and transactional rollback
  - every mart's edge cases
  - each DQ check, with a test showing it catches its defect
  - the publish gate
  - dashboard rendering, including escaping and missing-data display
  - the live app's queries, and its UI driven headlessly (filters, SQL box, refresh, missing
    warehouse)

  CI runs lint, strict mypy and the tests on Python 3.10 and 3.12 (DuckDB 1.x and 2.x),
  including the app extra.

### Adding a country or indicator

Edit [config/pipeline.yaml](config/pipeline.yaml) and run `make run`. No code changes are needed:

```yaml
countries: all                # or an explicit list, e.g. [BRA, CAN, FRA]
focus_countries: [BRA, ...]   # dashboard default and comparison-year basis; must be in scope
indicators:
  - code: EN.POP.DNST
    short_name: population_density
    valid_min: 0              # optional plausible range, checked as FAIL
    aggregation: mean         # how income groups combine it: mean | sum | ratio | weighted_mean
```

The request plan, the spine, the dimensions and the marts all derive from configuration. In
`all` mode, ingest fetches the country list first, keeps the economies (aggregates such as
"World" or "High income" carry region id `NA` and are dropped), and requests observations for
exactly those. A new economy therefore enters on the next run.

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
8. **Latest year:** both a common comparison year (like-for-like ranking) and each country's own
   latest year, labelled where they differ. The common year is the latest year in which every
   *focus* country has a value: with ~200 economies some are always missing (no year has GDP
   for all 217), so a common year across all of them wouldn't exist. Ranks and shares then
   cover every economy with a value in that year.
9. **Income groups:** each indicator's aggregation is set in config. Population and GDP are
   summed, GDP per capita is ΣGDP/Σpop, and life expectancy is population-weighted. The simple
   mean, country counts and `is_complete` are reported alongside, because a total over part of
   a group isn't comparable.
10. **DQ as data:** checks are YAML + SQL with fail/warn severity, persisted per execution.
    `report` only publishes a build whose latest check passed.
11. **Historical income groups:** each country-year uses the World Bank classification based
    on that year's data, from its OGHIST workbook. API v2 only knows the current group. Years
    the workbook doesn't cover yet use the current group, flagged in `income_group_source`;
    the current edition covers data up to 2025, so all of 2018–2024 is historical. Group
    membership therefore changes over time.
12. **Skip unchanged runs:** before a full fetch, ingest probes the API with 6 small requests
    (the country list, a 1-row page per indicator, the workbook). If the scope fingerprint,
    the country metadata hash, every indicator's `lastupdated` and row count, and the workbook
    hash all match the last successful run, it records a skip instead of re-fetching.
    Transform, check and report still run, because they're cheap and a code change may need
    a rebuild.

### Beyond the brief, on purpose

The brief's scope is nine countries. The pipeline deliberately goes further: it ingests every
economy the API lists (217), keeping the brief's nine as *focus countries* that the dashboards
and the comparison year are built around. There were two reasons:
- **A larger dataset for testing.** The nine countries have complete data for 2018–2024, so on
  its own the brief never exercises missing values, incomplete income groups, cross-indicator
  inconsistencies or a few hundred rows per indicator. With every economy, all of those occur in
  real data.
- **Personal curiosity:** seeing how the brief's countries sit among all economies.

It also uses a second World Bank source. The brief asks for the public API only, but API v2 has
no historical income classification, so it comes from the World Bank's public OGHIST workbook
(no key). It is landed verbatim like the API pages, and can be switched off in
`config/pipeline.yaml` (`income_history.enabled: false`). Every year then uses the current
group, as the API alone allows. If the download fails, the run carries on with current groups
and a data-quality check warns.

To reproduce exactly the brief's scope, set `countries` in `config/pipeline.yaml` to the nine
ISO3 codes; nothing else changes.

### Assumptions

- **Income group for year Y is the classification based on year-Y data**, published the
  following July (the workbook's fiscal year Y+2 is kept as `fiscal_year`). A year the
  workbook doesn't cover yet uses the current group. "Not classified" (e.g. Venezuela in recent
  years) is kept as its own group, `INX`, not dropped.
- "Latest available year" means the latest year with a non-null value, limited to the
  configured range (2018–2024).
- Values are stored as DOUBLE. Population and GDP are well within exact double precision for
  this use.
- The source's `obs_status`, `unit` and `decimal` are carried through but not interpreted.
- Plausibility bounds (`valid_min` / `valid_max`) are meant to catch *impossible* values, not
  extreme ones. Life expectancy's floor is 10 because real crisis years go below 20 (the WDI
  has the Central African Republic at 18.8 in 2022).
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
- The World Bank updates its indicators a few times a year. Ingest already probes cheaply
  (country list hash, a 1-row page per indicator for `lastupdated` and row count, workbook
  hash) and skips the fetch when nothing changed, recording it in `raw.ingest_skip`. In
  production the orchestrator would short-circuit the remaining tasks on a skip, and alert if
  skips continue past the expected release cadence.
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
    DuckDB 2.0; the Docker image was built and run end to end;
  - mart numbers were cross-checked against values computed independently in the tests.
- **Bugs this process caught:** a window function evaluated before row filtering in the
  latest-year view; test helpers that silently discarded earlier fixture edits; and, found by
  running the Docker image, a dashboard query pattern that DuckDB 2.x rejects (the image
  resolves DuckDB 2.x, local development used 1.x).

---

## Unfinished work and next steps

- **A `--years` override** for ad-hoc backfills without editing the config.
- **The OGHIST link could move.** It's the "Historical classification by income" link on the
  World Bank's Country and Lending Groups page, which currently serves the latest edition. If
  it moves, the run falls back to current groups (`income_history_loaded` warns). If it keeps
  serving an old edition, `income_history_up_to_date` warns. Either way, update
  `income_history.url`.
- **The live app isn't in the Docker image**, which stays pipeline-only. It would need the
  `app` extra and a published port.
- **dbt port** of the SQL layer if this moves to a warehouse (see the mapping above).
