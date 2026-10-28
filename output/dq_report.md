# Data-quality report

- Ingest run checked: `20261028T095204Z-b16cc5`
- Checked at: 2026-10-28 09:52:04 UTC
- Result: 15 pass, 0 warn, 0 fail, 0 error

| Check | Severity | Status | Violations | Description |
|---|---|---|---|---|
| `fct_grain_unique` | fail | **PASS** | 0 | At most one fact row per country x indicator x year. |
| `fct_spine_complete` | fail | **PASS** | 0 | The fact has exactly the configured country x indicator x year combinations, none missing and none extra (missing values are rows with a status, not absent rows). |
| `fct_dimension_integrity` | fail | **PASS** | 0 | Every fact row joins to its country, indicator and year dimension rows. |
| `source_duplicate_observations` | fail | **PASS** | 0 | The API returned more than one observation for the same country x indicator x year (the load keeps the first; the source needs investigating). |
| `source_unparseable_values` | fail | **PASS** | 0 | Observations whose year or value could not be parsed. They would otherwise look like missing data. |
| `fct_status_consistent` | fail | **PASS** | 0 | value_status agrees with the row: present <=> value not null; not_returned <=> no source response. |
| `fct_lineage_resolves` | fail | **PASS** | 0 | Every sourced fact row points to a raw response that belongs to the ingest run the core layer was built from. |
| `value_in_valid_range` | fail | **PASS** | 0 | Values lie within the plausible range configured per indicator. |
| `gdp_per_capita_consistent` | warn | **PASS** | 0 | GDP / population matches reported GDP per capita within the configured tolerance (see quality.v_gdp_per_capita_consistency). |
| `values_missing` | warn | **PASS** | 0 | Country x indicator x year combinations without a value (null in source or not returned). Expected for recent years; reported so the gaps are visible. |
| `dim_country_described` | fail | **PASS** | 0 | Every configured country was described by the API, including its income group (needed for the income-group comparison). |
| `dim_indicator_described` | warn | **PASS** | 0 | Every configured indicator was described by the API. |
| `source_unexpected_observations` | warn | **PASS** | 0 | The API returned observations outside the configured scope (ignored by the load). |
| `source_freshness` | warn | **PASS** | 0 | The World Bank's lastupdated date is known and was no older than max_source_age_days when the data was fetched. |
| `ingest_failed_since_checked_run` | warn | **PASS** | 0 | No ingest run has failed since the run being checked; otherwise the published data is older than intended. |
