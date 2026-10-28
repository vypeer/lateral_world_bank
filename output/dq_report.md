# Data-quality report

- Ingest run checked: `20261028T103723Z-660aca`
- Checked at: 2026-10-28 10:37:25 UTC
- Result: 14 pass, 2 warn, 0 fail, 0 error

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
| `gdp_per_capita_consistent` | warn | **WARN** | 35 | GDP / population matches reported GDP per capita within the configured tolerance (see quality.v_gdp_per_capita_consistency). |
| `values_missing` | warn | **WARN** | 128 | Country x indicator x year combinations without a value (null in source or not returned). Expected for recent years; reported so the gaps are visible. |
| `dim_country_described` | fail | **PASS** | 0 | Every country in scope was described by the API, including its income group (needed for the income-group comparison; "not classified" counts as described). |
| `focus_countries_in_scope` | fail | **PASS** | 0 | Every configured focus country is in scope (in 'all' mode: listed by the API as an economy), so dashboards and the comparison year are based on real countries. |
| `dim_indicator_described` | warn | **PASS** | 0 | Every configured indicator was described by the API. |
| `source_unexpected_observations` | warn | **PASS** | 0 | The API returned observations outside the configured scope (ignored by the load). |
| `source_freshness` | warn | **PASS** | 0 | The World Bank's lastupdated date is known and was no older than max_source_age_days when the data was fetched. |
| `ingest_failed_since_checked_run` | warn | **PASS** | 0 | No ingest run has failed since the run being checked; otherwise the published data is older than intended. |

## Details

### `gdp_per_capita_consistent` (warn)

Sample (first 5 of 35):

```json
{"country_iso3": "MAR", "year": 2018, "gdp_usd": 127341147581.818, "population": 35839760.0, "reported_gdp_per_capita": 3501.69799804688, "derived_gdp_per_capita": 3553.0692053132607, "relative_difference": 0.014670370573085878}
{"country_iso3": "CYP", "year": 2021, "gdp_usd": 30372637513.1352, "population": 1317309.0, "reported_gdp_per_capita": 32937.8359375, "derived_gdp_per_capita": 23056.577851616592, "relative_difference": 0.2999971857481236}
{"country_iso3": "MAR", "year": 2023, "gdp_usd": 146036093666.762, "population": 37712505.0, "reported_gdp_per_capita": 3813.72607421875, "derived_gdp_per_capita": 3872.351986874433, "relative_difference": 0.015372344923250037}
{"country_iso3": "MAR", "year": 2021, "gdp_usd": 142022058447.231, "population": 36954442.0, "reported_gdp_per_capita": 3785.93627929688, "derived_gdp_per_capita": 3843.166092109603, "relative_difference": 0.015116422620655358}
{"country_iso3": "CYP", "year": 2023, "gdp_usd": 35075440602.5202, "population": 1344976.0, "reported_gdp_per_capita": 36623.70703125, "derived_gdp_per_capita": 26078.859847700034, "relative_difference": 0.2879240808297297}
```

### `values_missing` (warn)

Sample (first 5 of 128):

```json
{"country_iso3": "SSD", "indicator_code": "NY.GDP.MKTP.CD", "year": 2018, "value_status": "null_in_source"}
{"country_iso3": "GIB", "indicator_code": "NY.GDP.MKTP.CD", "year": 2022, "value_status": "null_in_source"}
{"country_iso3": "MAF", "indicator_code": "NY.GDP.MKTP.CD", "year": 2023, "value_status": "null_in_source"}
{"country_iso3": "SMR", "indicator_code": "NY.GDP.MKTP.CD", "year": 2024, "value_status": "null_in_source"}
{"country_iso3": "YEM", "indicator_code": "NY.GDP.MKTP.CD", "year": 2024, "value_status": "null_in_source"}
```
