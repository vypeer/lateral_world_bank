-- Q3: How do the selected countries compare for the latest available year of each indicator?
--
-- Two notions of "latest", both shown and labelled:
--   latest_common_year   the most recent year in which every FOCUS country has a value (every
--                        country in scope when no focus list is configured): a like-for-like
--                        comparison year. Rank, share and ratio cover all countries with a value
--                        in that year; with ~200 economies some are always missing, which is
--                        why the year is anchored on the focus set.
--   country_latest_year  each country's own most recent value, which may be from different
--                        years; is_same_as_common_year flags where it differs.
CREATE OR REPLACE VIEW marts.v_latest_comparison AS
WITH basis AS (
    -- the countries that must all have a value: the focus list, else everything in scope
    SELECT country_iso3 FROM core.dim_country
    WHERE is_focus OR NOT EXISTS (SELECT 1 FROM core.dim_country WHERE is_focus)
),
common_year AS (
    SELECT indicator_code, max(year) AS latest_common_year
    FROM (
        SELECT indicator_code, year
        FROM marts.v_indicator_value
        WHERE country_iso3 IN (SELECT country_iso3 FROM basis)
        GROUP BY indicator_code, year
        HAVING count(*) FILTER (WHERE has_value) = (SELECT count(*) FROM basis)
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
        any_value(v.current_income_group_name) AS income_group_name,  -- current classification
        any_value(v.is_focus) AS is_focus,
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
    is_focus,
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
