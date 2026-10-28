-- Q2: Year-over-year change for each indicator, handling missing and zero prior values.
--
-- Strict YoY compares with exactly the previous calendar year. When it cannot be computed,
-- yoy_status says why and the change columns are NULL (never 0):
--   ok                  both values present
--   current_missing     this year has no value
--   prior_missing       previous year has no value (see the gap-aware columns)
--   prior_out_of_scope  previous year is before the configured range
--   prior_zero          previous value is 0: absolute change is given, percentage is not
-- Percentages are in percent and use |prior| as the base, so a fall is always negative.
--
-- Gap-aware columns compare with the most recent earlier year that has a value, and
-- annualise the change over the gap (compound rate) so it is comparable with strict YoY.
CREATE OR REPLACE VIEW marts.v_yoy_change AS
WITH series AS (
    SELECT
        v.*,
        p.value AS prior_value,
        p.has_value AS prior_has_value,
        -- last earlier year with a value (looks back past gaps)
        max(CASE WHEN v.has_value THEN v.year END) OVER w AS last_year_with_value,
        arg_max(CASE WHEN v.has_value THEN v.value END, CASE WHEN v.has_value THEN v.year END)
            OVER w AS last_value
    FROM marts.v_indicator_value AS v
    LEFT JOIN marts.v_indicator_value AS p
        ON p.country_iso3 = v.country_iso3
       AND p.indicator_code = v.indicator_code
       AND p.year = v.year - 1
    WINDOW w AS (
        PARTITION BY v.country_iso3, v.indicator_code ORDER BY v.year
        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    )
)
SELECT
    country_iso3,
    country_name,
    indicator,
    indicator_code,
    year,
    value,
    value_status,
    year - 1 AS prior_year,
    prior_value,
    CASE
        WHEN NOT has_value THEN 'current_missing'
        WHEN prior_has_value IS NULL THEN 'prior_out_of_scope'
        WHEN NOT prior_has_value THEN 'prior_missing'
        WHEN prior_value = 0 THEN 'prior_zero'
        ELSE 'ok'
    END AS yoy_status,
    CASE WHEN has_value AND prior_has_value THEN value - prior_value END AS yoy_abs_change,
    CASE
        WHEN has_value AND prior_has_value AND prior_value <> 0
            THEN 100 * (value - prior_value) / abs(prior_value)
    END AS yoy_pct_change,
    -- gap-aware: vs the last earlier year with a value
    CASE WHEN has_value THEN last_year_with_value END AS last_available_year,
    CASE WHEN has_value THEN last_value END AS last_available_value,
    CASE WHEN has_value THEN year - last_year_with_value END AS years_since_last_available,
    CASE WHEN has_value THEN value - last_value END AS abs_change_since_last_available,
    CASE
        WHEN has_value AND last_value > 0 AND value > 0
            THEN 100 * (pow(value / last_value, 1.0 / (year - last_year_with_value)) - 1)
    END AS annualized_pct_change_since_last_available
FROM series;
