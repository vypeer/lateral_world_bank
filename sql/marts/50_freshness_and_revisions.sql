-- Extra: where the data comes from and how current it is, per indicator.
CREATE OR REPLACE VIEW marts.v_data_freshness AS
WITH t AS (
    SELECT * FROM core.transform_run ORDER BY transformed_at DESC LIMIT 1
)
SELECT
    i.short_name AS indicator,
    f.indicator_code,
    max(f.source_lastupdated) AS source_lastupdated,
    max(f.year) FILTER (WHERE f.value_status = 'present') AS latest_year_with_any_value,
    count(*) FILTER (WHERE f.value_status = 'present') AS values_present,
    count(*) AS values_expected,
    any_value(t.ingest_run_id) AS ingest_run_id,
    any_value(r.finished_at) AS fetched_at,
    any_value(t.transformed_at) AS transformed_at
FROM core.fct_indicator_value AS f
JOIN core.dim_indicator AS i USING (indicator_code)
CROSS JOIN t
JOIN raw.ingest_run AS r ON r.run_id = t.ingest_run_id
GROUP BY i.short_name, f.indicator_code;

-- Extra: every source revision detected across runs (value changed, filled in or withdrawn).
CREATE OR REPLACE VIEW marts.v_source_revisions AS
SELECT
    h.country_iso3,
    h.indicator_code,
    h.year,
    prev.value AS previous_value,
    prev.value_status AS previous_status,
    h.value AS new_value,
    h.value_status AS new_status,
    h.valid_from_run_id AS detected_in_run_id,
    h.valid_from AS detected_at
FROM core.fct_indicator_value_history AS h
LEFT JOIN core.fct_indicator_value_history AS prev
    ON prev.country_iso3 = h.country_iso3
   AND prev.indicator_code = h.indicator_code
   AND prev.year = h.year
   AND prev.valid_to = h.valid_from
WHERE h.change_type = 'revision';
