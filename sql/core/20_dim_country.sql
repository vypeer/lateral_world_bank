-- Country dimension: one row per configured country (business key: ISO3).
-- Driven by config so a country the API did not describe still appears (with NULL
-- attributes, flagged by data-quality checks). Income group is the CURRENT World Bank
-- classification; the API exposes no history, so it is applied to all years (type 1).
CREATE OR REPLACE TABLE core.dim_country (
    country_iso3        VARCHAR PRIMARY KEY,
    country_iso2        VARCHAR,
    country_name        VARCHAR,
    region_id           VARCHAR,
    region_name         VARCHAR,
    income_group_id     VARCHAR,
    income_group_name   VARCHAR,
    income_group_rank   INTEGER,   -- 1 = low ... 4 = high, for ordering
    lending_type_id     VARCHAR,
    lending_type_name   VARCHAR,
    capital_city        VARCHAR,
    longitude           DOUBLE,
    latitude            DOUBLE,
    config_order        INTEGER NOT NULL,
    response_id         VARCHAR    -- lineage: raw.api_response the attributes came from
);

INSERT INTO core.dim_country
SELECT
    c.country_iso3,
    s.country_iso2,
    s.country_name,
    s.region_id,
    s.region_name,
    s.income_group_id,
    s.income_group_name,
    CASE s.income_group_id WHEN 'LIC' THEN 1 WHEN 'LMC' THEN 2 WHEN 'UMC' THEN 3 WHEN 'HIC' THEN 4 END,
    s.lending_type_id,
    s.lending_type_name,
    s.capital_city,
    s.longitude,
    s.latitude,
    c.config_order,
    s.response_id
FROM config.country AS c
LEFT JOIN (
    SELECT * FROM staging.stg_country
    QUALIFY row_number() OVER (PARTITION BY country_iso3 ORDER BY response_id) = 1
) AS s USING (country_iso3)
ORDER BY c.config_order;
