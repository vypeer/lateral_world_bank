-- One row per country returned by the country endpoint, flattened and trimmed.
CREATE OR REPLACE TABLE staging.stg_country AS
WITH rows AS (
    SELECT response_id, unnest(CAST(json_extract(payload, '$[1]') AS JSON[])) AS c
    FROM staging.current_response
    WHERE endpoint = 'country'
)
SELECT
    c ->> '$.id'                                         AS country_iso3,
    nullif(trim(c ->> '$.iso2Code'), '')                 AS country_iso2,
    nullif(trim(c ->> '$.name'), '')                     AS country_name,
    nullif(trim(c ->> '$.region.id'), '')                AS region_id,
    nullif(trim(c ->> '$.region.value'), '')             AS region_name,
    nullif(trim(c ->> '$.incomeLevel.id'), '')           AS income_group_id,
    nullif(trim(c ->> '$.incomeLevel.value'), '')        AS income_group_name,
    nullif(trim(c ->> '$.lendingType.id'), '')           AS lending_type_id,
    nullif(trim(c ->> '$.lendingType.value'), '')        AS lending_type_name,
    nullif(trim(c ->> '$.capitalCity'), '')              AS capital_city,
    try_cast(nullif(c ->> '$.longitude', '') AS DOUBLE)  AS longitude,
    try_cast(nullif(c ->> '$.latitude', '') AS DOUBLE)   AS latitude,
    response_id
FROM rows;
