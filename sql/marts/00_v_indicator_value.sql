-- Analyst-facing base view: one row per configured country x indicator x year, with readable
-- names and the reason a value is missing. Every other mart builds on this view.
-- income_group_* is the group FOR THAT YEAR (historical classification where available, see
-- core.dim_country_income_year); current_income_group_* is today's classification.
CREATE SCHEMA IF NOT EXISTS marts;

CREATE OR REPLACE VIEW marts.v_indicator_value AS
SELECT
    f.country_iso3,
    c.country_name,
    c.region_name,
    g.income_group_id,
    g.income_group_name,
    g.income_group_rank,
    g.income_group_source,
    c.income_group_id AS current_income_group_id,
    c.income_group_name AS current_income_group_name,
    f.indicator_code,
    i.short_name AS indicator,
    i.indicator_name,
    f.year,
    f.value,
    f.value_status,
    f.value_status = 'present' AS has_value,
    f.obs_status,
    f.response_id,
    f.source_lastupdated,
    f.last_changed_run_id,
    c.is_focus,
    c.config_order AS country_order,
    i.config_order AS indicator_order
FROM core.fct_indicator_value AS f
JOIN core.dim_country AS c USING (country_iso3)
JOIN core.dim_indicator AS i USING (indicator_code)
JOIN core.dim_country_income_year AS g USING (country_iso3, year);
