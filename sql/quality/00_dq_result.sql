-- Results of every data-quality check execution. Append-only, so check history is kept.
CREATE SCHEMA IF NOT EXISTS quality;

CREATE TABLE IF NOT EXISTS quality.dq_result (
    checked_at       TIMESTAMPTZ NOT NULL,
    ingest_run_id    VARCHAR NOT NULL,   -- the run the checked core layer was built from
    check_name       VARCHAR NOT NULL,
    severity         VARCHAR NOT NULL CHECK (severity IN ('fail', 'warn')),
    status           VARCHAR NOT NULL CHECK (status IN ('pass', 'warn', 'fail', 'error')),
    violation_count  BIGINT,
    sample           JSON,               -- up to 5 violating rows
    description      VARCHAR NOT NULL,
    error            VARCHAR,
    PRIMARY KEY (checked_at, check_name)
);
