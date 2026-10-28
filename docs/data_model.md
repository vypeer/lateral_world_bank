# Data model

The warehouse is a single DuckDB file (`data/warehouse.duckdb`) with one schema per layer.
Data only flows forward: `raw` → `staging` → `core` → `marts`.

| Schema    | Purpose                                                        | Written by        | Load style                     |
|-----------|----------------------------------------------------------------|-------------------|--------------------------------|
| `raw`     | API responses exactly as received, plus run bookkeeping        | `wbpipe ingest`   | append-only, content-deduped   |
| `config`  | The configured countries / indicators / years                  | `wbpipe transform`| replaced each transform        |
| `staging` | Responses of one ingest run, parsed into typed rows            | `wbpipe transform`| replaced each transform        |
| `core`    | Conformed dimensions and the indicator fact (+ its history)    | `wbpipe transform`| dims replaced; fact upserted   |
| `quality` | Data-quality results and diagnostic views                      | `wbpipe check`    | results appended; views        |
| `marts`   | Analyst-facing answers to the reporting questions              | `wbpipe transform`| views (exported by `report`)   |

## Entity relationships

```mermaid
erDiagram
    ingest_run ||--o{ api_fetch : "saw"
    api_response ||--o{ api_fetch : "seen in"
    api_response ||--o{ dim_country : "lineage"
    api_response ||--o{ dim_indicator : "lineage"
    api_response ||--o{ fct_indicator_value : "lineage"
    dim_country ||--o{ fct_indicator_value : "country_iso3"
    dim_indicator ||--o{ fct_indicator_value : "indicator_code"
    dim_year ||--o{ fct_indicator_value : "year"
    dim_country ||--|{ dim_country_income_year : "group per year"
    dim_year ||--|{ dim_country_income_year : "year"
    fct_indicator_value ||--|{ fct_indicator_value_history : "versions of"

    ingest_run {
        varchar run_id PK
        varchar status "succeeded | failed"
        varchar config_fingerprint
    }
    api_response {
        varchar response_id PK "sha256(request_url + payload_sha256)"
        varchar endpoint "country | indicator | observation"
        varchar request_url
        int page
        date source_lastupdated
        varchar payload_sha256
        json payload "verbatim body"
    }
    api_fetch {
        varchar run_id PK
        varchar response_id PK
        varchar landing_path
    }
    dim_country {
        varchar country_iso3 PK
        varchar country_name
        varchar region_name
        varchar income_group_id "LIC | LMC | UMC | HIC (current)"
        int income_group_rank
    }
    dim_indicator {
        varchar indicator_code PK
        varchar short_name UK "from config"
        varchar indicator_name
    }
    dim_year {
        int year PK
    }
    dim_country_income_year {
        varchar country_iso3 PK
        int year PK
        varchar income_group_id "year's group; INX = not classified"
        varchar income_group_source "historical | current"
        varchar fiscal_year
    }
    fct_indicator_value {
        varchar country_iso3 PK
        varchar indicator_code PK
        int year PK
        double value
        varchar value_status "present | null_in_source | not_returned"
        varchar row_hash
        varchar response_id FK
        varchar last_changed_run_id
    }
    fct_indicator_value_history {
        varchar country_iso3 PK
        varchar indicator_code PK
        int year PK
        timestamptz valid_from PK
        timestamptz valid_to
        boolean is_current
        varchar change_type "initial | revision"
    }
```

## Tables

### `raw.ingest_run` — one row per ingest attempt
Business key `run_id` (`<UTC timestamp>-<random>`). Failed runs are recorded with their error
and register nothing else, so downstream layers only ever see complete snapshots.

### `raw.api_response` — one row per distinct API response page
Business key `response_id` = sha256 of the request URL and the payload hash. The same page with
the same content is therefore stored once, however often it is fetched; a changed page gets a
new id. The payload is the response body verbatim; the same bytes are also on disk at
`data/landing/<run_id>/<endpoint>/[<indicator>/]page_NNNN.json`.

### Income history and skipped runs in `raw`
The OGHIST workbook is landed verbatim (`data/landing/<run_id>/income_history/OGHIST.xlsx`) and
registered in `raw.api_response` as an API-shaped page of parsed rows (`endpoint =
'income_history'`). The workbook's sha256 is kept in the page's meta. `raw.ingest_skip` records
each run skipped because probes showed the source unchanged.

### `raw.api_fetch` — which responses each successful run saw
Grain: run × response. This is how a run's complete snapshot is reconstructed even when most of
its responses were first stored by an earlier run.

### `staging.*` — one ingest run, parsed
`current_response` holds the responses of the run being transformed (default: latest successful
run; any older run can be replayed). `stg_country`, `stg_indicator` and `stg_observation` flatten
the JSON into typed, trimmed columns, keeping `response_id` on every row. `stg_observation` also
keeps `value_raw`, so a value that fails to parse is detectable instead of looking missing.
Staging is not deduplicated or scoped; that happens in `core`.

### `core.dim_country` — grain: country in scope
Business key `country_iso3`. Built from `config.country` left-joined to the API metadata, so a
configured country is present even if the API did not describe it (attributes NULL, flagged by
data-quality checks). With `countries: all`, `config.country` is every economy in the ingested
country metadata: aggregates, marked with region id `NA`, are excluded. It's derived after
staging, from the same rule ingest used to choose which countries to request. `is_focus` marks
the configured `focus_countries`, which dashboards show by default and which anchor the
comparison year; focus countries come first in `config_order`. Its income group is the
**current** classification (type 1); the yearly one is in `core.dim_country_income_year`.

### `core.dim_country_income_year` — grain: country × year
Business key `(country_iso3, year)`. The income group a country had for each year:
- **From the World Bank's historical classification (OGHIST workbook)** where it covers the
  year: the group based on that year's GNI per capita, published the following July
  (`fiscal_year` keeps the World Bank fiscal year, e.g. FY20 for 2018 data).
- **The current API group otherwise**, i.e. for years the workbook doesn't cover yet, or every
  year when the workbook is disabled or unavailable.

`income_group_source` (`historical` / `current`) says which applies; `source_code` keeps the
workbook's own code. "Not classified" (`INX`) is a real historical state and is kept. The
marts' `income_group_*` columns use this table; `current_income_group_*` keeps today's
group.

### `core.dim_indicator` — grain: configured indicator
Business key `indicator_code`; `short_name` (from config) is the analyst-friendly alias and is
unique too.

### `core.dim_year` — grain: configured year

### `core.fct_indicator_value` — grain: country × indicator × year
Business key `(country_iso3, indicator_code, year)`; each column is a foreign key to its dimension.

- **Complete spine.** Every configured combination has exactly one row, whether or not the API
  returned it. `value_status` says why `value` may be NULL:
  - `present`: the API returned a value.
  - `null_in_source`: the API returned the row with `value: null`.
  - `not_returned`: the API did not return the row at all.

  Missing data is never coerced to 0.
- **Lineage.** `response_id` points to the `raw.api_response` the value was read from, and from
  there to the landing file and request URL. It is NULL only for `not_returned` rows.
- **Idempotent upsert.** `row_hash` covers the business content (value, status, obs_status, unit,
  decimals). A row is written only when its hash changes, so re-running on unchanged data writes
  nothing. If a page changes because of *another* row, the unaffected rows only get their
  `response_id` refreshed. That is a lineage update, not a revision.
- **Scope follows config.** Adding a country, indicator or year adds spine rows on the next
  transform. Removing one deletes its fact rows. Its history is closed, not deleted.

### `core.fct_indicator_value_history` — SCD type 2 of the fact
One row per version of a fact row. `valid_from` / `valid_to` are transform timestamps, and
`valid_from_run_id` / `valid_to_run_id` name the ingest runs. `change_type` is `initial` for the
first version of a key and `revision` afterwards (source corrections, values filled in late,
values withdrawn). A closed version without a successor means the key left the configured scope.

### `core.transform_run` — one row per transform
Which ingest run the core layer was built from (`ingest_run_id`), when, and how many fact versions
were opened or closed. Data-quality checks run against the latest row.

## Marts (reporting layer)

All marts are views over `core`, so they are always consistent with the current fact.
`wbpipe report` exports each one to `output/` as CSV, but only after `wbpipe check` has
passed for the current transform.

| View | Answers | Grain |
|---|---|---|
| `v_indicator_value` | Base for everything else: readable names, the year's income group (plus today's), `value_status`, `has_value` | country × indicator × year |
| `v_country_year_wide` | One column per configured indicator, plus `indicators_missing`. Generated by the transform from config, because DuckDB can't use a dynamic `PIVOT` in a view | country × year |
| `v_missing_values` | **Q1** Which combinations are missing, why (`null_in_source` / `not_returned`), and the kind of gap (`gap_in_series`, `not_yet_published` (trailing gap, likely publication lag), `no_data_for_series`) | missing country × indicator × year |
| `v_series_completeness` | **Q1** How complete each series is: counts by status, `missing_years`, `complete` / `incomplete` / `empty` | country × indicator |
| `v_yoy_change` | **Q2** Strict YoY vs the previous calendar year, with `yoy_status` explaining any NULL (`current_missing`, `prior_missing`, `prior_out_of_scope`, `prior_zero`). Plus gap-aware change vs the last available year, annualised | country × indicator × year |
| `v_latest_comparison` | **Q3** `latest_common_year` (latest year every *focus* country has a value; every country when no focus list is set) with rank, share of total and ratio to average among all economies with a value that year; plus each country's own latest year and value, flagged when it differs | indicator × country |
| `v_income_group_by_year` | **Q4** Configured aggregate per income group (sum / ratio / weighted mean / mean), simple mean, min and max, country counts, `is_complete` | income group × indicator × year |
| `v_data_freshness` | Extra: source `lastupdated`, coverage and the published ingest run per indicator | indicator |
| `v_source_revisions` | Extra: every detected source revision, with previous and new value | revision |

Conventions:
- **Missing is never 0.** Change columns are NULL when they can't be computed, and a status
  column says why.
- **Percentages are in percent**, using `|prior|` as the base. When the prior value is 0, the
  percentage is NULL and the absolute change is still given.
- **Income-group aggregation is configured per indicator** in `pipeline.yaml`:
  - population and GDP are summed;
  - GDP per capita is ΣGDP / Σpopulation, using only countries that have both;
  - life expectancy is population-weighted;
  - unconfigured indicators default to the simple mean.

  A group-year is `is_complete` only when every member contributed. A sum over part of a
  group isn't comparable across years.
- **Income group is the one that applied in each year** (historical classification), so group
  membership changes over time; `income_group_source` marks years that use the current group.

## Data quality

Checks are declared in `config/dq_checks.yaml`. Each one is a SQL query that returns the
violating rows. `wbpipe check` runs all of them after every transform:

- **Severity `fail`** stops the pipeline (exit code 1) before outputs are published. Examples:
  grain or spine violations, broken dimension links, unparseable or out-of-range values, lineage
  that doesn't resolve to the checked ingest run, a configured country the API didn't describe.
- **Severity `warn`** is recorded and reported, and the pipeline continues. Examples: GDP /
  population vs GDP per capita outside tolerance, missing values, out-of-scope rows from the API,
  stale source data, an ingest failure since the checked run.
- A check whose SQL errors gets status `error` and always blocks, because an unknown result is
  not a pass.

Results are appended to `quality.dq_result` (one row per check per execution) and written to
`output/dq_results.csv` and `output/dq_report.md` (with sample violating rows).
`quality.v_gdp_per_capita_consistency` shows the cross-indicator comparison for every
country-year, with its relative difference and status.

Per-indicator plausible ranges (`valid_min` / `valid_max`) and the consistency rule live in
`config/pipeline.yaml`. Quality settings are not part of the scope fingerprint, so tuning a
threshold does not count as a scope change.

## Adding a country or indicator
Edit `config/pipeline.yaml` and run `make run`. No code or SQL changes: the request plan, the
spine and the dimensions are all derived from configuration.
