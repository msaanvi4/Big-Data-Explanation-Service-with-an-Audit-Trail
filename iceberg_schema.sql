-- Proposed Apache Spark SQL / Iceberg DDL. Not applied to a running catalog.
-- Use synthetic records only. Validate constraints in the write pipeline.

CREATE NAMESPACE IF NOT EXISTS audit;

CREATE TABLE IF NOT EXISTS audit.model_registry (
  model_version STRING,
  artifact_uri STRING,
  artifact_sha256 STRING,
  framework STRING,
  runtime_version STRING,
  feature_code_commit STRING,
  preprocessing_sha256 STRING,
  training_dataset_id STRING,
  training_dataset_version STRING,
  training_dataset_sha256 STRING,
  training_window_start DATE,
  training_window_end DATE,
  label_definition STRING,
  threshold_policy_version STRING,
  explainer_compatibility STRING,
  registered_at TIMESTAMP
) USING iceberg
TBLPROPERTIES ('format-version'='2', 'write.format.default'='parquet',
               'write.parquet.compression-codec'='zstd');

CREATE TABLE IF NOT EXISTS audit.prediction_events (
  prediction_id STRING,
  decision_ts TIMESTAMP,
  decision_date DATE,
  demo_decision STRING,
  risk_score DOUBLE,
  model_version STRING,
  input_snapshot_id STRING,
  explanation_id STRING,
  request_trace_id STRING,
  schema_version STRING
) USING iceberg
PARTITIONED BY (days(decision_ts))
TBLPROPERTIES ('format-version'='2', 'write.format.default'='parquet',
               'write.parquet.compression-codec'='zstd');

CREATE TABLE IF NOT EXISTS audit.input_snapshots (
  input_snapshot_id STRING,
  prediction_id STRING,
  feature_schema_version STRING,
  loan_amnt DECIMAL(12,2),
  revenue DECIMAL(12,2),
  dti_n DECIMAL(7,4),
  fico_n INT,
  experience_c BOOLEAN,
  emp_length STRING,
  purpose STRING,
  home_ownership_n STRING,
  input_hash STRING,
  created_at TIMESTAMP
) USING iceberg
PARTITIONED BY (days(created_at))
TBLPROPERTIES ('format-version'='2', 'write.format.default'='parquet',
               'write.parquet.compression-codec'='zstd');

CREATE TABLE IF NOT EXISTS audit.explanation_records (
  explanation_id STRING,
  prediction_id STRING,
  explainer_name STRING,
  explainer_version STRING,
  explanation_json STRING,
  reason_codes ARRAY<STRING>,
  rendered_summary STRING,
  explanation_hash STRING,
  created_at TIMESTAMP
) USING iceberg
PARTITIONED BY (days(created_at))
TBLPROPERTIES ('format-version'='2', 'write.format.default'='parquet',
               'write.parquet.compression-codec'='zstd');
