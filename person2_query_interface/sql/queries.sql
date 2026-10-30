-- Person 2: Query interface for the Explanation Service with Audit Trail.
-- All queries run against the Person 1 Iceberg schema (namespace `audit`):
--   audit.prediction_events, audit.input_snapshots,
--   audit.explanation_records, audit.model_registry
--
-- Dialect: Spark SQL / Iceberg. Where a live Iceberg catalog is unavailable,
-- the same SQL is executed by the DuckDB-based repository over the monthly
-- Parquet batches under person2_query_interface/data/monthly_batches/.
--
-- Privacy boundary (inherited from Person 1): the audit tables do NOT store
-- real customer IDs or direct borrower identifiers. The customer-facing
-- reference is request_trace_id (a synthetic tracing token). A "customer ID"
-- from the task brief therefore maps to request_trace_id.

-- ---------------------------------------------------------------------------
-- Q1. "Why was this decision made?" — lookup a decision by customer reference
--     and time window. Returns the winning (most recent) prediction for the
--     given request_trace_id within [from_ts, to_ts].
-- ---------------------------------------------------------------------------
SELECT
  p.prediction_id,
  p.decision_ts,
  p.decision_date,
  p.demo_decision,
  p.risk_score,
  p.model_version,
  p.input_snapshot_id,
  p.explanation_id,
  p.request_trace_id,
  p.schema_version
FROM audit.prediction_events p
WHERE p.request_trace_id = :request_trace_id
  AND p.decision_ts >= CAST(:from_ts AS TIMESTAMP)
  AND p.decision_ts <= CAST(:to_ts AS TIMESTAMP)
ORDER BY p.decision_ts DESC
LIMIT 1;

-- ---------------------------------------------------------------------------
-- Q2. Full explanation bundle for a prediction — the complete audit answer:
--     event + input snapshot + explanation + model build.
-- ---------------------------------------------------------------------------
SELECT
  p.prediction_id,
  p.decision_ts,
  p.decision_date,
  p.demo_decision,
  p.risk_score,
  p.model_version,
  p.request_trace_id,
  p.schema_version,
  i.input_snapshot_id,
  i.feature_schema_version,
  i.loan_amnt,
  i.revenue,
  i.dti_n,
  i.fico_n,
  i.experience_c,
  i.emp_length,
  i.purpose,
  i.home_ownership_n,
  i.input_hash,
  i.created_at AS input_created_at,
  e.explanation_id,
  e.explainer_name,
  e.explainer_version,
  e.explanation_json,
  e.reason_codes,
  e.rendered_summary,
  e.explanation_hash,
  e.created_at AS explanation_created_at,
  m.artifact_uri,
  m.artifact_sha256,
  m.framework,
  m.runtime_version,
  m.feature_code_commit,
  m.preprocessing_sha256,
  m.training_dataset_id,
  m.training_dataset_version,
  m.training_dataset_sha256,
  m.training_window_start,
  m.training_window_end,
  m.label_definition,
  m.threshold_policy_version,
  m.explainer_compatibility,
  m.registered_at
FROM audit.prediction_events p
JOIN audit.input_snapshots i ON i.input_snapshot_id = p.input_snapshot_id
JOIN audit.explanation_records e ON e.explanation_id = p.explanation_id
JOIN audit.model_registry m ON m.model_version = p.model_version
WHERE p.prediction_id = :prediction_id;

-- ---------------------------------------------------------------------------
-- Q3. Point-in-time (time-travel) lookup — retrieve the row exactly as it was
--     at Iceberg snapshot :snapshot_id (or as-of timestamp :as_of_ts).
--     Iceberg syntax; DuckDB backend emulates this via immutable monthly
--     batch snapshots recorded in batch_manifest.json.
-- ---------------------------------------------------------------------------
SELECT *
FROM audit.prediction_events VERSION AS OF :snapshot_id
WHERE p.prediction_id = :prediction_id;

-- ---------------------------------------------------------------------------
-- Q4. Decision consistency (regulator/compliance audit): did the same inputs
--     always produce the same decision across model versions and time?
--     Any input_hash with more than one distinct demo_decision is an
--     inconsistency requiring investigation.
-- ---------------------------------------------------------------------------
SELECT
  i.input_hash,
  COUNT(*) AS prediction_count,
  COUNT(DISTINCT p.demo_decision) AS distinct_decisions,
  COLLECT_LIST(DISTINCT p.demo_decision) AS decisions_seen,
  COUNT(DISTINCT p.model_version) AS model_versions_used,
  MIN(p.decision_ts) AS first_seen,
  MAX(p.decision_ts) AS last_seen
FROM audit.prediction_events p
JOIN audit.input_snapshots i ON i.input_snapshot_id = p.input_snapshot_id
GROUP BY i.input_hash
ORDER BY prediction_count DESC;

-- ---------------------------------------------------------------------------
-- Q5. Decision consistency violations only (used by compliance report).
-- ---------------------------------------------------------------------------
SELECT *
FROM (
  SELECT
    i.input_hash,
    COUNT(*) AS prediction_count,
    COUNT(DISTINCT p.demo_decision) AS distinct_decisions,
    COUNT(DISTINCT p.model_version) AS model_versions_used
  FROM audit.prediction_events p
  JOIN audit.input_snapshots i ON i.input_snapshot_id = p.input_snapshot_id
  GROUP BY i.input_hash
)
WHERE distinct_decisions > 1;

-- ---------------------------------------------------------------------------
-- Q6. Audit completeness for a prediction: verify every event resolves to
--     exactly one input snapshot, one explanation, and one model build.
-- ---------------------------------------------------------------------------
SELECT
  p.prediction_id,
  CASE WHEN i.input_snapshot_id IS NULL THEN 'MISSING_INPUT' ELSE 'OK' END AS input_status,
  CASE WHEN e.explanation_id IS NULL THEN 'MISSING_EXPLANATION' ELSE 'OK' END AS explanation_status,
  CASE WHEN m.model_version IS NULL THEN 'MISSING_MODEL' ELSE 'OK' END AS model_status
FROM audit.prediction_events p
LEFT JOIN audit.input_snapshots i ON i.input_snapshot_id = p.input_snapshot_id
LEFT JOIN audit.explanation_records e ON e.explanation_id = p.explanation_id
LEFT JOIN audit.model_registry m ON m.model_version = p.model_version
WHERE p.request_trace_id = :request_trace_id;

-- ---------------------------------------------------------------------------
-- Q7. Monthly batch discovery — which batch files cover a timestamp range?
--     Used by the repository to prune Parquet files (bytes scanned).
-- ---------------------------------------------------------------------------
SELECT batch_month, path, record_count, min_ts, max_ts, snapshot_id
FROM audit.batch_manifest
WHERE max_ts >= CAST(:from_ts AS TIMESTAMP)
  AND min_ts <= CAST(:to_ts AS TIMESTAMP)
ORDER BY batch_month;
