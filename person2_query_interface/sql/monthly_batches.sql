-- Person 2: Monthly batch layout used to simulate production snapshots.
-- Data is organized under data/monthly_batches/YYYY-MM/ as immutable Parquet
-- files, one file per Person 1 table, plus a batch_manifest.json that records
-- row counts, time range, and the Iceberg-equivalent snapshot id. This lets
-- the query interface prune files (bytes scanned) and emulate point-in-time
-- reads: "read the batch as it existed at snapshot_id".

CREATE TABLE IF NOT EXISTS audit.batch_manifest (
  batch_month STRING,                 -- e.g. 2026-10
  table_name STRING,                  -- prediction_events | input_snapshots | explanation_records | model_registry
  path STRING,                        -- relative Parquet path within the batch folder
  record_count BIGINT,
  min_ts TIMESTAMP,
  max_ts TIMESTAMP,
  snapshot_id STRING,                 -- Iceberg snapshot id equivalent for this batch write
  written_at TIMESTAMP,
  schema_version STRING
) USING iceberg
TBLPROPERTIES ('format-version'='2', 'write.format.default'='parquet',
               'write.parquet.compression-codec'='zstd');

-- Example: point-in-time read emulation over monthly batches
-- SELECT * FROM audit.prediction_events
-- WHERE batch_month = (SELECT batch_month FROM audit.batch_manifest
--                      WHERE snapshot_id = :snapshot_id LIMIT 1)
--   AND prediction_id = :prediction_id;
