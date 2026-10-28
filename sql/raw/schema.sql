-- Raw layer: API responses exactly as received, plus the runs that fetched them.
-- Owned by the ingest step; nothing here is parsed or typed beyond envelope metadata.

CREATE SCHEMA IF NOT EXISTS raw;

-- One row per pipeline ingest attempt (succeeded or failed).
CREATE TABLE IF NOT EXISTS raw.ingest_run (
    run_id              VARCHAR PRIMARY KEY,
    started_at          TIMESTAMPTZ NOT NULL,
    finished_at         TIMESTAMPTZ,
    status              VARCHAR NOT NULL CHECK (status IN ('succeeded', 'failed')),
    config_fingerprint  VARCHAR NOT NULL,
    n_pages             INTEGER,
    n_new_responses     INTEGER,
    error               VARCHAR
);

-- One row per distinct response. response_id is content-addressed
-- (request URL + payload hash), so re-fetching identical data adds no row here.
CREATE TABLE IF NOT EXISTS raw.api_response (
    response_id         VARCHAR PRIMARY KEY,
    endpoint            VARCHAR NOT NULL,   -- country | indicator | observation
    entity              VARCHAR,            -- indicator code for indicator/observation endpoints
    request_url         VARCHAR NOT NULL,
    params              JSON NOT NULL,
    page                INTEGER NOT NULL,
    pages               INTEGER NOT NULL,
    total_rows          INTEGER NOT NULL,
    http_status         INTEGER NOT NULL,
    source_lastupdated  DATE,
    payload_sha256      VARCHAR NOT NULL,
    payload             JSON NOT NULL,
    first_run_id        VARCHAR NOT NULL,
    first_fetched_at    TIMESTAMPTZ NOT NULL,
    landing_path        VARCHAR NOT NULL    -- relative to the landing dir, from the first run
);

-- Which responses each successful run saw. Downstream layers read the latest
-- successful run's responses through this table.
CREATE TABLE IF NOT EXISTS raw.api_fetch (
    run_id              VARCHAR NOT NULL,
    response_id         VARCHAR NOT NULL,
    fetched_at          TIMESTAMPTZ NOT NULL,
    landing_path        VARCHAR NOT NULL,
    PRIMARY KEY (run_id, response_id)
);
