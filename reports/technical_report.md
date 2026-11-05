# Technical Report: Explanation Service with an Audit Trail

**Big Data Systems and Responsible AI — Mini Project**
**Version:** 1.0.0  
**Date:** 2026-11-05  
**Authors:** Person 1 (Storage Design), Person 2 (Query Interface & Reproducibility), Person 3 (Regulatory Compliance & Audit Trail)

---

## Abstract

This report documents a complete, auditable loan-decision explanation service built on Apache Iceberg-backed storage, a versioned reproducibility engine, and a regulatory compliance layer. Given a synthetic customer reference and time window, the service answers "why was this decision made?" by returning the full audit bundle — prediction event, model input snapshot, explanation, and model build — and can replay any historical decision to verify that the same inputs reproduce the same score, decision, and explanation. The system enforces a strict privacy boundary (no borrower identifiers are stored), supports point-in-time reads via Iceberg time travel, and is validated by a 27-test pytest suite, Person 3 compliance tests, and latency benchmarks that meet a 5-second SLO with wide margin.

---

## 1. Introduction

Automated lending decisions are increasingly subject to transparency and accountability requirements. Regulators and customers need to know not only what decision was made, but why, under which model version, on what inputs, and whether the decision can be reproduced. This project delivers an **Explanation Service with an Audit Trail** that captures each lending decision as an immutable, versioned audit event and exposes it through a query interface, a reproducibility engine, and a compliance reporting layer.

The work is a three-person collaboration:

- **Person 1 — Storage Design:** Apache Iceberg table schema (`iceberg_schema.sql`), partitioning rationale, sample synthetic records (`synthetic_audit_records.json`), and warehouse initialization (`init_iceberg.py`).
- **Person 2 — Query Interface & Reproducibility:** FastAPI service layer, SQL queries Q1–Q7, versioned deterministic model and explainers, reproducibility engine, pytest suite (27 tests), and performance benchmarks.
- **Person 3 — Regulatory Compliance & Audit Trail:** Role-based access control, SHA-256 hash-chained audit logging, compliance report generator, right-to-be-forgotten deletion workflow, and chain-of-custody ledger.

---

## 2. System Architecture

### 2.1 Logical data flow

```
Synthetic loan application
        │
        ▼
Versioned feature transform  (15 curated features, deterministic encoding)
        │
        ▼
Versioned model + explainer  (pinned by version + SHA-256 digest)
        │
        ▼
Prediction event  (risk score + demo decision: APPROVE / REVIEW / DECLINE)
        │
        ▼
Four immutable Iceberg audit tables  (Parquet + ZSTD, time-partitioned)
        │
        ▼
Query / replay / compliance  (FastAPI, DuckDB or Iceberg backend)
```

### 2.2 Storage layer (Person 1)

Four Iceberg tables in namespace `audit`, format version 2, Parquet with Zstandard compression:

| Table | Role | Partition |
|-------|------|-----------|
| `prediction_events` | Audit anchor, one row per scoring call | `days(decision_ts)` |
| `input_snapshots` | Canonical post-transform model inputs | `days(created_at)` |
| `explanation_records` | Ranked contributions + reason codes | `days(created_at)` |
| `model_registry` | Immutable model build metadata + digest | (dimension table) |

Rows are linked by UUIDs (`prediction_id`, `input_snapshot_id`, `explanation_id`, `model_version`, `request_trace_id`). Iceberg does not enforce uniqueness or foreign keys, so referential integrity is validated at write time. Each input and explanation carries a SHA-256 hash (`input_hash`, `explanation_hash`) computed over canonical sorted JSON for tamper detection.

Corrections append new records (optionally `supersedes_prediction_id`) rather than overwriting history, and Iceberg snapshots are retained through the audit window to enable point-in-time replay (`VERSION AS OF`).

### 2.3 Privacy boundary

Per the course brief, no real personal data is stored:

- `SK_ID_CURR` (the Home Credit borrower identifier) is read only transiently during batch generation to derive deterministic UUID5s; it is never written to any audit table.
- No raw ZIP codes, free-text fields (`title`, `desc`), or loan IDs enter the model or audit payload.
- The customer-facing reference is `request_trace_id`, a synthetic tracing token.
- All decisions are *demonstration* decisions; the Home Credit `TARGET` label is a final repayment outcome, not a historical accept/deny decision.

### 2.4 Query interface (Person 2)

A backend-agnostic FastAPI service (`person2_query_interface/app/service.py`) with two storage backends:

- **DuckDB/Parquet (default):** reads monthly Parquet batches directly; partition pruning via `batch_manifest.json` bounds bytes scanned; no cluster required.
- **Apache Iceberg (production-like):** real catalog via `pyiceberg` (SQLite/SQL catalog), native `VERSION AS OF` time travel. Initialize with `init_iceberg.py`.

Endpoints:

| Endpoint | Purpose | SQL |
|----------|---------|-----|
| `GET /health` | Service health + backend | — |
| `GET /explain?request_trace_id=&from_ts=&to_ts=` | "Why was this decision made?" — most recent decision for a trace in a window, plus observability (latency, bytes scanned, SLO flag) | Q1 |
| `GET /decision/{id}` | Full audit bundle: event + input + explanation + model | Q2 |
| `GET /decision/{id}/at/{snapshot}` | Point-in-time bundle (Iceberg time-travel equivalent) | Q3 |
| `GET /replay/{id}` | Reproducibility replay + validation | Reproducibility engine |
| `GET /consistency` | Decision-consistency report (same inputs → same decision?) | Q5 |

SQL queries Q1–Q7 (`sql/queries.sql`) run identically against Iceberg and, through a DuckDB translation layer, against the Parquet batches. Q4/Q5 detect input hashes with more than one distinct decision; Q6 verifies every event resolves to exactly one input, explanation, and model; Q7 drives batch discovery and partition pruning.

### 2.5 Reproducibility engine (Person 2)

`ReproducibilityEngine.replay_prediction(prediction_id)`:

1. Reads the audit bundle (exact `model_version`, input snapshot, explainer name/version).
2. Loads the pinned deterministic model artifact (weights derived from a version-keyed seed; artifact SHA-256 recorded per `model_registry`).
3. Re-computes `input_hash` (SHA-256 of canonical sorted model inputs) and verifies it matches storage.
4. Re-runs the model; score must match within `1e-6`, decision must match exactly under `demo-thresholds-v1` (<0.40 APPROVE, 0.40–0.70 REVIEW, ≥0.70 DECLINE).
5. Regenerates the explanation with the recorded explainer version and compares reason codes, contribution ranking, and `explanation_hash`.

Three versioned explainers are registered at `1.0.0` — `feature_importance`, `shap`, `lime` — deterministic shims of the real methods; unregistered (method, version) pairs are rejected, so every audit explanation is tied to an exact, reproducible method version.

### 2.6 Compliance & audit trail (Person 3)

Person 3 integrates with Person 2's repository and reproducibility engine:

- **Audit logging:** every actor action is recorded (actor, role, customer reference, prediction ID, action, purpose, timestamp, status SUCCESS/DENIED/FAILED, reason) and SHA-256 hash-chained to the previous event, making tampering detectable.
- **Role-based access control:** Customer / Regulator / Admin. Customers see only their own synthetic reference; cross-customer access is denied and logged. Regulators can view, verify, and replay. Admins have wildcard prototype permission.
- **Compliance report:** `COMPLIANT` only when decision consistency passes, every evaluated prediction is reproducible, audit records are complete, and the audit hash chain is valid. Reported `COMPLIANT` on 3,000 synthetic predictions with 0 consistency violations.
- **Right-to-be-forgotten:** prototype deletion workflow retaining pseudonymous deletion evidence (request ID, subject token, timestamps, record count, status, evidence hash).
- **Chain of custody:** per-prediction hash chain across 8 lifecycle event types (`PREDICTION_CREATED` … `PREDICTION_EXPORTED`).
- **Integration adapter:** wires authorization, audit logging, and custody recording onto Person 2's access and replay operations.

---

## 3. Model

The service uses pinned, deterministic logistic-regression demo models rather than externally downloaded binaries:

| Artifact | Value |
|----------|-------|
| Model versions | `loan-risk-demo-v1.0.0` (Oct-2026 batch), `loan-risk-demo-v1.1.0` (Nov/Dec-2026 batches) |
| Framework | deterministic-logistic-demo (sigmoid over scaled, deviation-from-baseline features) |
| Runtime | python-3.12-scikit-learn-1.9.1 (declared) |
| Threshold policy | `demo-thresholds-v1`: <0.40 APPROVE; 0.40–0.70 REVIEW; ≥0.70 DECLINE |
| Features (14) | income_total, credit_amount, annuity, dti_ratio, credit_score_proxy, days_birth, days_employed, gender_enc, owns_car_enc, owns_realty_enc, children_count, education_enc, housing_enc, occupation_enc |
| Explainers | feature_importance 1.0.0, shap 1.0.0, lime 1.0.0 |
| Tolerance | score match ≤ 1e-6; decision exact |

Each artifact records `artifact_uri`, `artifact_sha256`, feature-code commit, preprocessing digest, training dataset id/version/hash/window, label definition, threshold-policy version, explainer compatibility, and registration time — mirroring Person 1's `model_registry` contract.

---

## 4. Data

Source: Home Credit Default Risk training set (`application_train.csv`, 307,511 rows). A curated 15-feature subset is used; `SK_ID_CURR` is never stored. Derived fields include `dti_ratio = annuity / income_total` and `credit_score_proxy` from EXT_SOURCE_1/2/3 (median-imputed, scaled to 300–850); the `365243` unemployed sentinel is mapped to 0.

Three deterministic monthly Parquet batches are generated under `person2_query_interface/data/monthly_batches/`:

| Batch | Records | Model version | Period |
|-------|---------|---------------|--------|
| 2026-10 | 102,503 | loan-risk-demo-v1.0.0 | October |
| 2026-11 | ~102,503 | loan-risk-demo-v1.1.0 | November |
| 2026-12 | ~102,503 | loan-risk-demo-v1.1.0 | December |

Each batch contains 4 Zstandard-compressed Parquet files (one per audit table) plus `batch_manifest.json` (row counts, time range, snapshot id) enabling month-level pruning and point-in-time emulation. All IDs are UUID5s derived from namespace + row index; no borrower identifiers are persisted.

---

## 5. Evaluation

### 5.1 Correctness (tests)

- **Person 2 pytest:** 27 tests passing — Q1/Q2 lookups, point-in-time reads, audit completeness, explainer versioning/determinism, consistency behavior, replay of deterministic samples across all monthly batches (exact score, exact decision, hash verification), and FastAPI endpoints including the SLO check.
- **Person 3 unittest:** 39 unit tests + 5 integration tests = 44 passing — audit logging/tamper detection, authorization/ownership, compliance report logic, deletion evidence, custody hash verification, and cross-person integration (customer/regulator access and replay).

### 5.2 Reproducibility

Replay of stored predictions reproduces the stored risk score within `1e-6`, the demo decision exactly, the input hash, and the explanation hash; reason codes and contribution ranking match the audit trail. This holds across both model versions and all three explainer methods.

### 5.3 Performance (benchmarks, DuckDB/Parquet backend)

Target SLO: lookup within 5 seconds. 200 lookups per scale:

| Records | p50 latency | p95 latency | Bytes scanned | Within 5s SLO |
|---------|-------------|-------------|---------------|---------------|
| 1,000 | ~37 ms | ~69 ms | ~391 KB | yes |
| 5,000 | ~46 ms | ~65 ms | ~1.84 MB | yes |
| 10,000 | ~54 ms | ~88 ms | ~3.66 MB | yes |

The seconds-level response requirement is met with wide margin. On a production Iceberg cluster, partition pruning plus file compaction preserves this profile at larger volumes.

### 5.4 Compliance

Final compliance report on 3,000 synthetic predictions: **COMPLIANT** — decision consistency passed (0 violations), prediction reproducibility passed, audit completeness passed, audit trail integrity passed, and at least one prediction evaluated. Access summary: 8 events (5 successful, 3 denied, 0 failed).

---

## 6. Reproducibility of This Project

The full pipeline is scripted in `scripts/pipeline.sh`:

```
./scripts/pipeline.sh all
# install -> generate -> test -> serve -> demo -> benchmark
```

A `Dockerfile` and `docker-compose.yml` containerize the FastAPI service (DuckDB default; Iceberg via `EXPLANATION_REPO_BACKEND=iceberg`). Install dependencies with `pip install -r requirements.txt`. Results, benchmarks, and compliance logs are written to `.pipeline_logs/` and `person2_query_interface/benchmarks/results/`.

---

## 7. Limitations and Future Work

- Person 3's deletion workflow is backed by a prototype SQLite database and is not yet connected to Person 1's Iceberg storage or Person 2's Parquet files; coordinated deletion/expiration policies require legal review.
- Real SHAP/LIME libraries are not used; deterministic shims preserve the audit schema and reproducibility contract, with the real libraries swappable in later.
- The Iceberg `IcebergRepository` is implemented for the duckdb-parquet pattern; a production deployment would route Q1–Q7 through the live catalog rather than emulating snapshots.
- Authenticated identities and trusted role assignment are not yet integrated into Person 2's API endpoints.
- Compliance report export and persistent storage are planned improvements.

---

## 8. Conclusion

The Explanation Service with an Audit Trail demonstrates how immutable, versioned storage (Iceberg/Parquet + ZSTD), pinned model and explainer artifacts, SHA-256 hash chaining, and role-based access control combine to produce lending decisions that are explainable, reproducible, and auditable. The service meets its latency SLO, passes its full test suite, and returns a compliant regulatory report on synthetic data, while rigorously excluding direct borrower identifiers from the audit trail.
