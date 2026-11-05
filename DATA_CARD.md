# Data Card — Synthetic Loan Audit Data

**Project:** Explanation Service with an Audit Trail  
**Owner:** Person 1 (Storage Design) / Person 2 (Data Generation)  
**Version:** 1.0.0  
**Date:** 2026-11-05  
**Status:** Synthetic demonstration data

---

## 1. Dataset overview

Synthetic monthly loan-decision audit records derived from the public Home Credit Default Risk training set. The data is used to demonstrate an explainable, auditable, reproducible lending-decision service. No real borrower identifiers are stored.

| Field | Value |
|-------|-------|
| Source | Home Credit Default Risk (`application_train.csv`, 307,511 rows, Kaggle) |
| Curated rows used | ~307,511 across 3 monthly batches (2026-10 / 2026-11 / 2026-12) |
| Records per batch | 2026-10: 102,503; 2026-11 & 2026-12: ~102,503 each |
| Format | Apache Parquet with Zstandard compression; one file per audit table per batch |
| Layout | `person2_query_interface/data/monthly_batches/YYYY-MM/` + `batch_manifest.json` |
| Backend | DuckDB/Parquet (default) or Apache Iceberg (via `init_iceberg.py`) |

---

## 2. Source and provenance

- **Source record:** Home Credit Default Risk competition, Kaggle.
- **Curated columns (15):** `SK_ID_CURR`, `AMT_INCOME_TOTAL`, `AMT_CREDIT`, `AMT_ANNUITY`, `DAYS_BIRTH`, `DAYS_EMPLOYED`, `CODE_GENDER`, `FLAG_OWN_CAR`, `FLAG_OWN_REALTY`, `CNT_CHILDREN`, `NAME_EDUCATION_TYPE`, `NAME_HOUSING_TYPE`, `OCCUPATION_TYPE`, `EXT_SOURCE_1`, `EXT_SOURCE_2`, `EXT_SOURCE_3`.
- `SK_ID_CURR` is read only transiently to derive deterministic UUID5 identifiers; it is **never stored** in any audit table.
- `TARGET` (final repayment outcome: payment difficulties > X days on first Y installments) is a training label only and is never part of model input or an accept/deny decision.

---

## 3. Schema (Person 1's `audit.*` tables)

### prediction_events (partitioned by `days(decision_ts)`)
`prediction_id`, `decision_ts`, `decision_date`, `demo_decision` (APPROVE/REVIEW/DECLINE), `risk_score` [0,1], `model_version`, `input_snapshot_id`, `explanation_id`, `request_trace_id`, `schema_version`.

### input_snapshots (partitioned by `days(created_at)`)
`input_snapshot_id`, `prediction_id`, `feature_schema_version`, `income_total`, `credit_amount`, `annuity`, `dti_ratio`, `credit_score_proxy`, `days_birth`, `days_employed`, `gender`, `owns_car`, `owns_realty`, `children_count`, `education_type`, `housing_type`, `occupation_type`, `ext_source_1`, `ext_source_2`, `ext_source_3`, `input_hash` (SHA-256), `created_at`.

### explanation_records (partitioned by `days(created_at)`)
`explanation_id`, `prediction_id`, `explainer_name`, `explainer_version`, `explanation_json`, `reason_codes`, `rendered_summary`, `explanation_hash` (SHA-256), `created_at`.

### model_registry (dimension table)
`model_version`, `artifact_uri`, `artifact_sha256`, `framework`, `runtime_version`, `feature_code_commit`, `preprocessing_sha256`, `training_dataset_id`, `training_dataset_version`, `training_dataset_sha256`, `training_window_start`, `training_window_end`, `label_definition`, `threshold_policy_version`, `explainer_compatibility`, `registered_at`.

---

## 4. Derived fields

| Field | Derivation |
|-------|------------|
| `dti_ratio` | annuity / income_total (guarded ÷0 → 0.0), rounded to 4 decimals |
| `credit_score_proxy` | round(300 + mean(EXT_SOURCE_1/2/3) × 550), range 300–850; missing EXT_SOURCE median-imputed |
| `days_employed` | 365243 (Home Credit unemployed sentinel) → 0 |
| `input_hash` | SHA-256 of canonical sorted JSON of the 14 model inputs |
| `explanation_hash` | SHA-256 of explainer name/version + contributions + reason codes |
| IDs | UUID5 (`uuid.NAMESPACE_URL`, namespace + row index); `SK_ID_CURR` not stored |

---

## 5. Data generation

`person2_query_interface/data/generator.py` produces deterministic batches:

```bash
python3 person2_query_interface/data/generator.py
```

- Same CSV rows → same batch (deterministic timestamps, UUIDs, scores, decisions, explanations).
- Batch 2026-10 uses model `loan-risk-demo-v1.0.0`; batches 2026-11 and 2026-12 use `loan-risk-demo-v1.1.0` (model iteration).
- Each batch's `batch_manifest.json` records row counts, min/max timestamps, snapshot id, and written-at time, enabling month-level partition pruning and point-in-time reads.

---

## 6. Privacy and ethical handling

- **No direct identifiers:** `SK_ID_CURR`, raw ZIP, `title`, `desc`, and loan IDs are excluded.
- **Customer-facing reference:** `request_trace_id` (synthetic token), not a real customer key.
- **Synthetic use only:** all decisions are labeled *demonstration* decisions; not real credit outcomes.
- **Retention:** Iceberg snapshots retained through the audit window; no partitions on applicant ID or high-cardinality/sensitive fields.

---

## 7. Intended uses

- Demonstration of "why was this decision made?" lookup (`GET /explain`).
- Full audit-bundle retrieval (`GET /decision/{id}`).
- Point-in-time / time-travel reads (`GET /decision/{id}/at/{snapshot}`).
- Reproducibility replay and validation (`GET /replay/{id}`).
- Decision-consistency and compliance reporting (`GET /consistency`, Person 3 report).

---

## 8. Limitations

- Synthetic illustration of the Home Credit schema; not representative of a real loan portfolio.
- Placeholder SHA-256 digests in `synthetic_audit_records.json` (fixture only); generated batches compute real hashes.
- `TARGET` repayment label is not used as a decision label; no real accept/deny history is modeled.
