-- Income group per country and year (business key: country_iso3 + year).
--
-- From the World Bank's historical classification where it covers the year: the group based
-- on that year's GNI per capita (published the following July; the workbook's fiscal year is
-- kept). Years it does not cover yet (the latest data year), or every year when the workbook
-- is unavailable, use the country's CURRENT group from the API. income_group_source says which.
-- "Not classified" (INX) is a real historical state, e.g. Venezuela since 2021.
CREATE OR REPLACE TABLE core.dim_country_income_year (
    country_iso3         VARCHAR NOT NULL,
    year                 INTEGER NOT NULL,
    income_group_id      VARCHAR,
    income_group_name    VARCHAR,
    income_group_rank    INTEGER,           -- 1 = low ... 4 = high; NULL when not classified
    income_group_source  VARCHAR NOT NULL CHECK (income_group_source IN ('historical', 'current')),
    fiscal_year          VARCHAR,           -- e.g. FY20 for 2018 data; NULL for 'current'
    source_code          VARCHAR,           -- the workbook's code (L, LM, UM, H, '..', '')
    response_id          VARCHAR,           -- lineage: raw.api_response it came from
    PRIMARY KEY (country_iso3, year)
);

INSERT INTO core.dim_country_income_year
WITH history AS (
    SELECT * FROM staging.stg_income_history
    QUALIFY row_number() OVER (PARTITION BY country_iso3, data_year ORDER BY response_id) = 1
),
resolved AS (
    SELECT
        c.country_iso3,
        y.year,
        coalesce(h.income_group_id, c.income_group_id) AS income_group_id,
        CASE WHEN h.country_iso3 IS NOT NULL THEN 'historical' ELSE 'current' END AS source,
        h.fiscal_year,
        h.source_code,
        coalesce(h.response_id, c.response_id) AS response_id
    FROM core.dim_country AS c
    CROSS JOIN core.dim_year AS y
    LEFT JOIN history AS h ON h.country_iso3 = c.country_iso3 AND h.data_year = y.year
)
SELECT
    country_iso3,
    year,
    income_group_id,
    CASE income_group_id
        WHEN 'LIC' THEN 'Low income' WHEN 'LMC' THEN 'Lower middle income'
        WHEN 'UMC' THEN 'Upper middle income' WHEN 'HIC' THEN 'High income'
        WHEN 'INX' THEN 'Not classified'
    END,
    CASE income_group_id WHEN 'LIC' THEN 1 WHEN 'LMC' THEN 2 WHEN 'UMC' THEN 3 WHEN 'HIC' THEN 4 END,
    source,
    fiscal_year,
    source_code,
    response_id
FROM resolved;
