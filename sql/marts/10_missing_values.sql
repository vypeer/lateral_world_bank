-- Q1: Which country-indicator-year combinations are missing or incomplete?

-- Every combination without a value, with the reason and context for judging it.
CREATE OR REPLACE VIEW marts.v_missing_values AS
WITH ctx AS (
    SELECT
        *,
        max(year) FILTER (WHERE has_value)
            OVER (PARTITION BY country_iso3, indicator_code) AS last_year_with_value,
        count(*) FILTER (WHERE has_value)
            OVER (PARTITION BY country_iso3, indicator_code) AS years_with_value
    FROM marts.v_indicator_value
)
SELECT
    country_iso3,
    country_name,
    indicator,
    indicator_code,
    year,
    value_status AS missing_reason,       -- null_in_source | not_returned
    CASE
        WHEN years_with_value = 0 THEN 'no_data_for_series'
        WHEN year > last_year_with_value THEN 'not_yet_published'  -- trailing gap: likely lag
        ELSE 'gap_in_series'
    END AS gap_type,
    last_year_with_value
FROM ctx
WHERE NOT has_value;

-- Completeness of every country x indicator series over the configured years.
CREATE OR REPLACE VIEW marts.v_series_completeness AS
SELECT
    country_iso3,
    any_value(country_name) AS country_name,
    indicator,
    indicator_code,
    count(*) AS years_expected,
    count(*) FILTER (WHERE has_value) AS years_with_value,
    count(*) FILTER (WHERE value_status = 'null_in_source') AS years_null_in_source,
    count(*) FILTER (WHERE value_status = 'not_returned') AS years_not_returned,
    round(100.0 * count(*) FILTER (WHERE has_value) / count(*), 1) AS completeness_pct,
    min(year) FILTER (WHERE has_value) AS first_year_with_value,
    max(year) FILTER (WHERE has_value) AS last_year_with_value,
    coalesce(string_agg(CAST(year AS VARCHAR), ';' ORDER BY year) FILTER (WHERE NOT has_value), '')
        AS missing_years,
    CASE
        WHEN count(*) FILTER (WHERE has_value) = count(*) THEN 'complete'
        WHEN count(*) FILTER (WHERE has_value) = 0 THEN 'empty'
        ELSE 'incomplete'
    END AS completeness_status
FROM marts.v_indicator_value
GROUP BY country_iso3, indicator, indicator_code;
