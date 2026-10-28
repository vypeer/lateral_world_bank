-- Cross-indicator consistency: GDP / population should equal reported GDP per capita.
-- One row per country-year; the "inconsistent" rows are what the WARN check reports.
-- Empty when the rule is not configured (config.gdp_per_capita_rule has no row).
CREATE OR REPLACE VIEW quality.v_gdp_per_capita_consistency AS
WITH joined AS (
    SELECT
        g.country_iso3,
        g.year,
        g.value AS gdp_usd,
        p.value AS population,
        c.value AS reported_gdp_per_capita,
        g.value / nullif(p.value, 0) AS derived_gdp_per_capita,
        r.tolerance
    FROM config.gdp_per_capita_rule AS r
    JOIN core.fct_indicator_value AS g
        ON g.indicator_code = r.gdp_code
    JOIN core.fct_indicator_value AS p
        ON p.indicator_code = r.population_code
       AND p.country_iso3 = g.country_iso3 AND p.year = g.year
    JOIN core.fct_indicator_value AS c
        ON c.indicator_code = r.gdp_per_capita_code
       AND c.country_iso3 = g.country_iso3 AND c.year = g.year
)
SELECT
    *,
    abs(derived_gdp_per_capita - reported_gdp_per_capita)
        / nullif(abs(reported_gdp_per_capita), 0) AS relative_difference,
    CASE
        WHEN derived_gdp_per_capita IS NULL OR nullif(reported_gdp_per_capita, 0) IS NULL
            THEN 'not_comparable'
        WHEN abs(derived_gdp_per_capita - reported_gdp_per_capita)
             / abs(reported_gdp_per_capita) <= tolerance
            THEN 'consistent'
        ELSE 'inconsistent'
    END AS consistency_status
FROM joined;
