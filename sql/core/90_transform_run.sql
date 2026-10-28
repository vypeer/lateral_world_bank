-- One row per transform: which ingest run the core layer was built from, and what changed.
-- Data-quality checks and freshness reporting read the latest row.
CREATE TABLE IF NOT EXISTS core.transform_run (
    transformed_at   TIMESTAMPTZ PRIMARY KEY,
    ingest_run_id    VARCHAR NOT NULL,
    fact_rows        INTEGER NOT NULL,
    versions_opened  INTEGER NOT NULL,
    versions_closed  INTEGER NOT NULL
);

INSERT INTO core.transform_run
SELECT
    getvariable('transformed_at'),
    getvariable('run_id'),
    (SELECT count(*) FROM core.fct_indicator_value),
    (SELECT count(*) FROM core.fct_indicator_value_history
     WHERE valid_from = getvariable('transformed_at')),
    (SELECT count(*) FROM core.fct_indicator_value_history
     WHERE valid_to = getvariable('transformed_at'));
