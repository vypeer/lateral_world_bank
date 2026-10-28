-- One row per indicator metadata record.
CREATE OR REPLACE TABLE staging.stg_indicator AS
WITH rows AS (
    SELECT response_id, unnest(CAST(json_extract(payload, '$[1]') AS JSON[])) AS i
    FROM staging.current_response
    WHERE endpoint = 'indicator'
)
SELECT
    i ->> '$.id'                                   AS indicator_code,
    nullif(trim(i ->> '$.name'), '')               AS indicator_name,
    nullif(trim(i ->> '$.unit'), '')               AS unit,
    nullif(trim(i ->> '$.source.id'), '')          AS source_id,
    nullif(trim(i ->> '$.source.value'), '')       AS source_name,
    nullif(trim(i ->> '$.sourceNote'), '')         AS source_note,
    nullif(trim(i ->> '$.sourceOrganization'), '') AS source_organization,
    response_id
FROM rows;
