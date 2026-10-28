-- Indicator dimension: one row per configured indicator (business key: WB indicator code).
-- short_name comes from config and is the analyst-friendly column/series name.
CREATE OR REPLACE TABLE core.dim_indicator (
    indicator_code       VARCHAR PRIMARY KEY,
    short_name           VARCHAR NOT NULL UNIQUE,
    indicator_name       VARCHAR,
    unit                 VARCHAR,
    source_id            VARCHAR,
    source_name          VARCHAR,
    source_note          VARCHAR,
    source_organization  VARCHAR,
    config_order         INTEGER NOT NULL,
    response_id          VARCHAR
);

INSERT INTO core.dim_indicator
SELECT
    c.indicator_code,
    c.short_name,
    s.indicator_name,
    s.unit,
    s.source_id,
    s.source_name,
    s.source_note,
    s.source_organization,
    c.config_order,
    s.response_id
FROM config.indicator AS c
LEFT JOIN (
    SELECT * FROM staging.stg_indicator
    QUALIFY row_number() OVER (PARTITION BY indicator_code ORDER BY response_id) = 1
) AS s USING (indicator_code)
ORDER BY c.config_order;
