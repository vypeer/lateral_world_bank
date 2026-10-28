-- Year dimension: exactly the configured reporting years.
CREATE SCHEMA IF NOT EXISTS core;

CREATE OR REPLACE TABLE core.dim_year (
    year INTEGER PRIMARY KEY
);

INSERT INTO core.dim_year
SELECT year FROM config.year ORDER BY year;
