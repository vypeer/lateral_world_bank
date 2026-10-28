-- Responses seen by the ingest run being transformed (getvariable('run_id')).
-- Pinning staging to one run keeps every layer built from one consistent snapshot,
-- and lets an older run be replayed by passing its run_id.
CREATE SCHEMA IF NOT EXISTS staging;

CREATE OR REPLACE TABLE staging.current_response AS
SELECT
    r.response_id,
    r.endpoint,
    r.entity,
    r.request_url,
    r.page,
    r.source_lastupdated,
    r.payload
FROM raw.api_response AS r
JOIN raw.api_fetch AS f USING (response_id)
WHERE f.run_id = getvariable('run_id');
