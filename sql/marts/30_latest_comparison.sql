-- Q3: How do the selected countries compare for the latest available year of each indicator?
--
-- Two notions of "latest", both shown and labelled:
--   latest_common_year   the most recent year in which EVERY configured country has a value:
--                        a like-for-like comparison (same year for all). Rank uses this.
--   country_latest_year  each country's own most recent value, which may be from different
--                        years; is_same_as_common_year flags where it differs.
CREATE OR REPLACE VIEW marts.v_latest_comparison AS
WITH common_year AS (
    SELECT indicator_code, max(year) AS latest_common_year
    FROM (
        SELECT indicator_code, year
        FROM marts.v_indicator_value
        GROUP BY indicator_code, year
        HAVING count(*) FILTER (WHERE has_value) = (SELECT count(*) FROM core.dim_country)
    )
    GROUP BY indicator_code
),
per_country AS (
    -- exactly one row per indicator x country
    SELECT
        v.indicator_code,
        v.country_iso3,
        any_value(v.indicator) AS indicator,
        any_value(v.country_name) AS country_name,
        any_value(v.income_group_name) AS income_group_name,
        any_value(cy.latest_common_year) AS latest_common_year,
        any_value(v.value) FILTER (WHERE v.year = cy.latest_common_year) AS value_at_common_year,
        max(v.year) FILTER (WHERE v.has_value) AS country_latest_year,
        arg_max(v.value, v.year) FILTER (WHERE v.has_value) AS country_latest_value
    FROM marts.v_indicator_value AS v
    LEFT JOIN common_year AS cy USING (indicator_code)
    GROUP BY v.indicator_code, v.country_iso3
)
SELECT
    indicator,
    indicator_code,
    country_iso3,
    country_name,
    income_group_name,
    latest_common_year,
    value_at_common_year,
    CASE WHEN value_at_common_year IS NOT NULL THEN
        rank() OVER (PARTITION BY indicator_code ORDER BY value_at_common_year DESC NULLS LAST)
    END AS rank_at_common_year,
    100 * value_at_common_year / sum(value_at_common_year) OVER (PARTITION BY indicator_code)
        AS pct_of_selected_total_at_common_year,
    value_at_common_year / avg(value_at_common_year) OVER (PARTITION BY indicator_code)
        AS ratio_to_selected_average_at_common_year,
    country_latest_year,
    country_latest_value,
    country_latest_year IS NOT DISTINCT FROM latest_common_year AS is_same_as_common_year
FROM per_country;
