-- Historical income classification (OGHIST workbook), parsed at ingest into an API-shaped page.
-- One row per economy x data year; empty when the workbook was disabled or unavailable.
CREATE OR REPLACE TABLE staging.stg_income_history AS
WITH rows AS (
    SELECT response_id, unnest(CAST(json_extract(payload, '$[1]') AS JSON[])) AS h
    FROM staging.current_response
    WHERE endpoint = 'income_history'
)
SELECT
    h ->> '$.country_iso3'                  AS country_iso3,
    CAST(h ->> '$.data_year' AS INTEGER)    AS data_year,
    h ->> '$.fiscal_year'                   AS fiscal_year,
    h ->> '$.source_code'                   AS source_code,
    h ->> '$.income_group_id'               AS income_group_id,
    response_id
FROM rows;
