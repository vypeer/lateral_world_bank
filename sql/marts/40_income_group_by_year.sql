-- Q4: How do the indicators compare across income groups, by year?
--
-- Countries are grouped by their income group IN EACH YEAR (historical World Bank
-- classification; the latest year uses the current group until the history covers it), so group
-- membership can change between years; income_group_source says which classification applied.
-- Each indicator is aggregated as configured (config.indicator):
--   sum            sum of the group's values                 (population, GDP)
--   ratio          sum(numerator) / sum(denominator)         (GDP per capita = sum GDP / sum pop)
--   weighted_mean  sum(value * weight) / sum(weight)         (population-weighted life expectancy)
--   mean           simple average
-- simple_mean and the country counts are always given alongside. A group-year is
-- is_complete only when every country in the group contributed, because a sum or weighted
-- figure over a partial group is not comparable with other years.
CREATE OR REPLACE VIEW marts.v_income_group_by_year AS
WITH base AS (
    SELECT
        v.income_group_id,
        v.income_group_name,
        v.income_group_rank,
        v.income_group_source,
        v.country_iso3,
        v.year,
        v.indicator,
        v.indicator_code,
        v.value,
        i.aggregation,
        num.value AS numerator_value,
        den.value AS denominator_value,
        w.value AS weight_value
    FROM marts.v_indicator_value AS v
    JOIN config.indicator AS i USING (indicator_code)
    LEFT JOIN core.fct_indicator_value AS num
        ON num.indicator_code = i.agg_numerator
       AND num.country_iso3 = v.country_iso3 AND num.year = v.year
    LEFT JOIN core.fct_indicator_value AS den
        ON den.indicator_code = i.agg_denominator
       AND den.country_iso3 = v.country_iso3 AND den.year = v.year
    LEFT JOIN core.fct_indicator_value AS w
        ON w.indicator_code = i.agg_weight
       AND w.country_iso3 = v.country_iso3 AND w.year = v.year
),
-- which countries can contribute to the configured aggregate
contrib AS (
    SELECT
        *,
        CASE aggregation
            WHEN 'ratio' THEN numerator_value IS NOT NULL AND denominator_value IS NOT NULL
            WHEN 'weighted_mean' THEN value IS NOT NULL AND weight_value IS NOT NULL
            ELSE value IS NOT NULL
        END AS contributes
    FROM base
)
SELECT
    income_group_id,
    any_value(income_group_name) AS income_group_name,
    any_value(income_group_rank) AS income_group_rank,
    string_agg(DISTINCT income_group_source, ';') AS income_group_source,
    year,
    indicator,
    indicator_code,
    any_value(aggregation) AS aggregation_method,
    CASE any_value(aggregation)
        WHEN 'sum' THEN sum(value) FILTER (WHERE contributes)
        WHEN 'ratio' THEN sum(numerator_value) FILTER (WHERE contributes)
                          / nullif(sum(denominator_value) FILTER (WHERE contributes), 0)
        WHEN 'weighted_mean' THEN sum(value * weight_value) FILTER (WHERE contributes)
                                  / nullif(sum(weight_value) FILTER (WHERE contributes), 0)
        ELSE avg(value) FILTER (WHERE contributes)
    END AS aggregate_value,
    avg(value) AS simple_mean,
    min(value) AS min_value,
    max(value) AS max_value,
    count(*) AS countries_in_group,
    count(value) AS countries_with_value,
    count(*) FILTER (WHERE contributes) AS countries_in_aggregate,
    count(*) FILTER (WHERE contributes) = count(*) AS is_complete,
    string_agg(country_iso3, ';' ORDER BY country_iso3) AS countries
FROM contrib
GROUP BY income_group_id, year, indicator, indicator_code;
