-- Fact: one row per configured country x indicator x year (the "spine"), whether or not
-- the API returned a value. value_status says why a value is missing:
--   present         API returned a numeric value
--   null_in_source  API returned the row with value = null
--   not_returned    API did not return the row at all
--
-- Loaded incrementally: rows are only written when their content (row_hash) changes, so
-- re-running on unchanged source data rewrites nothing. Every content change is versioned
-- in core.fct_indicator_value_history (SCD type 2).

CREATE TABLE IF NOT EXISTS core.fct_indicator_value (
    country_iso3         VARCHAR NOT NULL,
    indicator_code       VARCHAR NOT NULL,
    year                 INTEGER NOT NULL,
    value                DOUBLE,
    value_status         VARCHAR NOT NULL
                         CHECK (value_status IN ('present', 'null_in_source', 'not_returned')),
    obs_status           VARCHAR,
    unit                 VARCHAR,
    decimal_places       INTEGER,
    row_hash             VARCHAR NOT NULL,   -- hash of the business content above
    response_id          VARCHAR,            -- lineage: latest raw.api_response holding this row
    source_lastupdated   DATE,
    first_loaded_run_id  VARCHAR NOT NULL,
    last_changed_run_id  VARCHAR NOT NULL,
    loaded_at            TIMESTAMPTZ NOT NULL,
    changed_at           TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (country_iso3, indicator_code, year)
);

CREATE TABLE IF NOT EXISTS core.fct_indicator_value_history (
    country_iso3         VARCHAR NOT NULL,
    indicator_code       VARCHAR NOT NULL,
    year                 INTEGER NOT NULL,
    value                DOUBLE,
    value_status         VARCHAR NOT NULL,
    obs_status           VARCHAR,
    unit                 VARCHAR,
    decimal_places       INTEGER,
    row_hash             VARCHAR NOT NULL,
    response_id          VARCHAR,            -- response this version first appeared in
    change_type          VARCHAR NOT NULL CHECK (change_type IN ('initial', 'revision')),
    valid_from_run_id    VARCHAR NOT NULL,
    valid_from           TIMESTAMPTZ NOT NULL,
    valid_to_run_id      VARCHAR,
    valid_to             TIMESTAMPTZ,        -- NULL while current; a closed version with no
    is_current           BOOLEAN NOT NULL,   -- successor means the key left the configured scope
    PRIMARY KEY (country_iso3, indicator_code, year, valid_from)
);

CREATE OR REPLACE TEMP TABLE fct_candidate AS
WITH spine AS (
    SELECT c.country_iso3, i.indicator_code, y.year
    FROM config.country AS c
    CROSS JOIN config.indicator AS i
    CROSS JOIN config.year AS y
),
obs AS (
    -- Duplicate rows for a key are a source defect (caught by a FAIL check);
    -- pick one deterministically so the load itself stays well-defined.
    SELECT * FROM staging.stg_observation
    QUALIFY row_number() OVER (
        PARTITION BY country_iso3, indicator_code, year ORDER BY page, row_in_page
    ) = 1
),
joined AS (
    SELECT
        s.country_iso3,
        s.indicator_code,
        s.year,
        o.value,
        CASE
            WHEN o.country_iso3 IS NULL THEN 'not_returned'
            WHEN o.value_raw IS NULL THEN 'null_in_source'
            ELSE 'present'
        END AS value_status,
        o.obs_status,
        o.unit,
        o.decimal_places,
        o.response_id,
        o.source_lastupdated
    FROM spine AS s
    LEFT JOIN obs AS o USING (country_iso3, indicator_code, year)
)
SELECT
    *,
    md5(concat_ws('|',
        value_status,
        coalesce(CAST(value AS VARCHAR), '<null>'),
        coalesce(obs_status, '<null>'),
        coalesce(unit, '<null>'),
        coalesce(CAST(decimal_places AS VARCHAR), '<null>')
    )) AS row_hash
FROM joined;

-- 1. History: close current versions whose content changed or whose key left the scope.
UPDATE core.fct_indicator_value_history AS h
SET valid_to = getvariable('transformed_at'),
    valid_to_run_id = getvariable('run_id'),
    is_current = false
WHERE h.is_current
  AND NOT EXISTS (
      SELECT 1 FROM fct_candidate AS c
      WHERE c.country_iso3 = h.country_iso3
        AND c.indicator_code = h.indicator_code
        AND c.year = h.year
        AND c.row_hash = h.row_hash
  );

-- 2. History: open a version for every key without a current one (new or just closed).
INSERT INTO core.fct_indicator_value_history
SELECT
    c.country_iso3, c.indicator_code, c.year,
    c.value, c.value_status, c.obs_status, c.unit, c.decimal_places, c.row_hash, c.response_id,
    CASE WHEN EXISTS (
        SELECT 1 FROM core.fct_indicator_value_history AS p
        WHERE p.country_iso3 = c.country_iso3
          AND p.indicator_code = c.indicator_code
          AND p.year = c.year
    ) THEN 'revision' ELSE 'initial' END,
    getvariable('run_id'), getvariable('transformed_at'), NULL, NULL, true
FROM fct_candidate AS c
WHERE NOT EXISTS (
    SELECT 1 FROM core.fct_indicator_value_history AS h
    WHERE h.is_current
      AND h.country_iso3 = c.country_iso3
      AND h.indicator_code = c.indicator_code
      AND h.year = c.year
);

-- 3. Current-state fact: upsert on the business key, touching only rows that changed.
MERGE INTO core.fct_indicator_value AS t
USING fct_candidate AS s
ON t.country_iso3 = s.country_iso3 AND t.indicator_code = s.indicator_code AND t.year = s.year
WHEN MATCHED AND t.row_hash <> s.row_hash THEN UPDATE SET
    value = s.value,
    value_status = s.value_status,
    obs_status = s.obs_status,
    unit = s.unit,
    decimal_places = s.decimal_places,
    row_hash = s.row_hash,
    response_id = s.response_id,
    source_lastupdated = s.source_lastupdated,
    last_changed_run_id = getvariable('run_id'),
    changed_at = getvariable('transformed_at')
-- Same content in a different response (e.g. another value on the page was revised):
-- refresh lineage only; this is not a revision.
WHEN MATCHED AND t.response_id IS DISTINCT FROM s.response_id THEN UPDATE SET
    response_id = s.response_id,
    source_lastupdated = s.source_lastupdated
WHEN NOT MATCHED THEN INSERT VALUES (
    s.country_iso3, s.indicator_code, s.year,
    s.value, s.value_status, s.obs_status, s.unit, s.decimal_places, s.row_hash,
    s.response_id, s.source_lastupdated,
    getvariable('run_id'), getvariable('run_id'),
    getvariable('transformed_at'), getvariable('transformed_at')
)
WHEN NOT MATCHED BY SOURCE THEN DELETE;

DROP TABLE fct_candidate;
