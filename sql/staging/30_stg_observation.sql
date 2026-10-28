-- One row per observation as returned by the API (not yet deduplicated or scoped).
-- value_raw is kept so a value that fails to parse is detectable rather than
-- silently becoming "missing".
CREATE OR REPLACE TABLE staging.stg_observation AS
WITH rows AS (
    SELECT
        response_id,
        source_lastupdated,
        page,
        unnest(CAST(json_extract(payload, '$[1]') AS JSON[])) AS o,
        generate_subscripts(CAST(json_extract(payload, '$[1]') AS JSON[]), 1) AS row_in_page
    FROM staging.current_response
    WHERE endpoint = 'observation'
)
SELECT
    o ->> '$.countryiso3code'                     AS country_iso3,
    nullif(o ->> '$.country.id', '')              AS country_iso2,
    o ->> '$.indicator.id'                        AS indicator_code,
    try_cast(o ->> '$.date' AS INTEGER)           AS year,
    o ->> '$.date'                                AS date_raw,
    try_cast(o ->> '$.value' AS DOUBLE)           AS value,
    o ->> '$.value'                               AS value_raw,
    nullif(o ->> '$.unit', '')                    AS unit,
    nullif(o ->> '$.obs_status', '')              AS obs_status,
    try_cast(o ->> '$.decimal' AS INTEGER)        AS decimal_places,
    response_id,
    page,
    row_in_page,
    source_lastupdated
FROM rows;
