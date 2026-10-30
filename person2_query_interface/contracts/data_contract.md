# Person 2 — Query Interface Data Contract

Contract between Person 1's storage layer (Iceberg `audit.*` tables,
Parquet + ZSTD, time-partitioned) and Person 2's query/reproducibility
layer. All queries live in `sql/queries.sql`.

## Source schema (Person 1)

`audit.prediction_events(prediction_id, decision_ts, decision_date,
demo_decision, risk_score, model_version, input_snapshot_id,
explanation_id, request_trace_id, schema_version)` partitioned by
`days(decision_ts)`.

`audit.input_snapshots(input_snapshot_id, prediction_id,
feature_schema_version, loan_amnt, revenue, dti_n, fico_n, experience_c,
emp_length, purpose, home_ownership_n, input_hash, created_at)`
partitioned by `days(created_at)`.

`audit.explanation_records(explanation_id, prediction_id, explainer_name,
explainer_version, explanation_json, reason_codes, rendered_summary,
explanation_hash, created_at)` partitioned by `days(created_at)`.

`audit.model_registry(model_version, artifact_uri, artifact_sha256,
framework, runtime_version, feature_code_commit, preprocessing_sha256,
training_dataset_id, training_dataset_version, training_dataset_sha256,
training_window_start, training_window_end, label_definition,
threshold_policy_version, explainer_compatibility, registered_at)`.

## Query contract

Q1 `find_decision_by_trace_and_time(request_trace_id, from_ts, to_ts) ->
PredictionEvent | None` — most recent decision for the customer reference
within the window. Privacy: `request_trace_id` is a synthetic token; no
real customer ID is stored or returned.

Q2 `get_decision_bundle(prediction_id) -> DecisionBundle` — event joined to
exactly one input snapshot, one explanation record, and one model build.
Invalid if any join is missing (reported by Q6).

Q3 `get_decision_bundle_at_snapshot(prediction_id, snapshot_id)` —
point-in-time read. DuckDB backend emulates Iceberg `VERSION AS OF` by
reading only the immutable monthly batch that owns `snapshot_id`.

Q4/Q5 `consistency_violations()` — input hashes with more than one distinct
`demo_decision`; the compliance report passes when the list is empty.

Q6 `audit_completeness(request_trace_id)` — per-event status:
input_status / explanation_status / model_status ∈ {OK, MISSING_*}.

## Reproducibility contract

Given a prediction, replay must reproduce:

- `risk_score` within `SCORE_TOLERANCE` (1e-6) of the stored value,
- `demo_decision` exactly, under the stored `threshold_policy_version`,
- reason codes and contribution ranking of the stored explanation
  (recorded `explainer_name` / `explainer_version`),
- `input_hash` = SHA-256(canonical sorted model inputs),
- `explanation_hash` reproducible from the regenerated explainer payload.

## Backend contract

`AuditRepository` is pluggable: `duckdb` (Parquet batches), `fixture`
(JSON), or a future Spark/Iceberg implementation of the same SQL. The
service layer is backend-agnostic.

## Versioning

Model versions are pinned artifacts with SHA-256 digests (Person 1's
`model_registry`). Explainer methods are versioned name + version pairs;
unregistered (method, version) pairs are rejected at explanation time.
