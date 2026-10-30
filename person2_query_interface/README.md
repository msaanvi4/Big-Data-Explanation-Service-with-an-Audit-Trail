# Person 2 — Query Interface & Reproducibility Engine

This directory is Person 2's submission for the *Explanation Service with an
Audit Trail* mini project. It builds on Person 1's Iceberg storage design
(`iceberg_schema.sql`, `person1_storage_design.md`) and provides:

1. **Query interface** — SQL + FastAPI application layer that answers "why
   was this decision made?" for a customer reference and timestamp.
2. **Reproducibility engine** — re-runs the pinned model version on the stored
   input snapshot and validates the explanation against the audit trail.
3. **Versioned explainers** — feature-importance / SHAP / LIME method versions
   recorded on every explanation record.
4. **Reproducibility test suite** — pytest tests (27 passing).
5. **Performance benchmarks** — p50/p95 latency, bytes scanned, rows returned
   vs. a 5-second SLO.

## Repository layout

```
person2_query_interface/
├── README.md                   # this file
├── sql/
│   ├── queries.sql             # Q1–Q7 against Person 1's audit.*. Iceberg tables
│   └── monthly_batches.sql     # monthly batch snapshot layout
├── app/
│   ├── config.py               # runtime configuration
│   ├── model.py                # pinned, deterministic model artifacts (weights by version)
│   ├── explainers.py           # versioned feature_importance / shap / lime
│   ├── repository.py           # pluggable audit repository (DuckDB/Parquet, fixture, Iceberg stub)
│   ├── reproducibility.py      # replay engine + consistency report
│   └── service.py              # FastAPI application layer
├── data/
│   ├── generator.py            # deterministic synthetic monthly batch generator
│   ├── synthetic_audit_records.json   # fixture for the JSON backend
│   └── monthly_batches/        # generated immutable monthly Parquet batches (2026-10, 2026-11, 2026-12)
├── tests/                      # pytest suite (27 tests)
│   ├── conftest.py
│   ├── test_query_interface.py
│   ├── test_reproducibility.py
│   ├── test_explainer_versioning.py
│   ├── test_consistency.py
│   └── test_service.py
├── benchmarks/
│   ├── run_benchmarks.py       # p50/p95 latency, bytes scanned, rows returned
│   └── results/                # benchmark output JSON (latest.json)
└── contracts/
    └── data_contract.md        # query interface contract summary
```

## Privacy boundary (from Person 1)

The audit tables do **not** store real customer IDs, direct borrower
identifiers, raw ZIP codes, or free text. The customer-facing reference is
`request_trace_id`, a synthetic tracing token. In this implementation "customer
ID" from the brief maps to `request_trace_id`.

## Query interface (SQL)

All queries in `sql/queries.sql` target Person 1's tables
`audit.prediction_events`, `audit.input_snapshots`,
`audit.explanation_records`, `audit.model_registry`.

| Query | Purpose |
|-------|---------|
| Q1 | Most recent decision for a `request_trace_id` within a time window ("why was this decision made?") |
| Q2 | Full audit bundle: event + input snapshot + explanation + model build |
| Q3 | Point-in-time read (`VERSION AS OF` equivalent over immutable monthly batches) |
| Q4 | Decision consistency across all inputs |
| Q5 | Consistency violations (same inputs, different decisions) |
| Q6 | Audit completeness: every event resolves to exactly one input, explanation, model |
| Q7 | Batch discovery / partition pruning by month |

The DuckDB repository prunes monthly Parquet batches via `batch_manifest.json`,
so bytes scanned are bounded by the time window; on a real Iceberg catalog the
same SQL runs natively with `VERSION AS OF` time travel.

## Application layer (FastAPI)

Run the service:

```bash
EXPLANATION_REPO_BACKEND=duckdb uvicorn person2_query_interface.app.service:app
```

Endpoints:

- `GET /explain?request_trace_id=...&from_ts=...&to_ts=...` — decision lookup
- `GET /decision/{prediction_id}` — full audit bundle
- `GET /decision/{prediction_id}/at/{snapshot_id}` — point-in-time bundle
- `GET /replay/{prediction_id}` — reproducibility replay
- `GET /consistency` — decision-consistency report
- `GET /health`

The `/explain` response includes observability fields (latency_ms,
bytes_scanned) and a `within_slo` flag against the 5-second SLO.

## Reproducibility engine

`ReproducibilityEngine.replay_prediction(prediction_id)`:

1. Reads the audit bundle: exact `model_version`, input snapshot, explainer
   name/version.
2. Loads the pinned model artifact (deterministic weights derived from the
   version string; artifact SHA-256 recorded per Person 1's `model_registry`).
3. Re-computes the input-hash (SHA-256 of canonical sorted model inputs).
4. Re-runs the model on the stored input; score must match within
   `1e-6` and the decision must match exactly.
5. Regenerates the explanation with the recorded explainer version and
   compares reason codes, contribution ranking, and explanation hash.

Explainer versions are registered in `app/explainers.py`
(`feature_importance 1.0.0`, `shap 1.0.0`, `lime 1.0.0`); unknown
method/version combinations are rejected, so every audit explanation is tied
to an exact, reproducible method version.

## Test suite

```bash
EXPLANATION_REPO_BACKEND=duckdb python3 -m pytest person2_query_interface/tests -v
```

27 tests pass: Q1–Q2 lookups, point-in-time reads, audit completeness,
explainer versioning/determinism, consistency-query behavior, replay of a
deterministic sample across all monthly batches (exact score, exact decision,
hash verification), and the FastAPI endpoints including the SLO check.

## Performance benchmarks

```bash
python3 person2_query_interface/benchmarks/run_benchmarks.py
```

Generates scale batches (1k / 5k / 10k predictions), runs 200 lookups per
scale, and writes `benchmarks/results/latest.json`. Results on this
workload (DuckDB/Parquet backend):

| Records | p50 latency | p95 latency | Bytes scanned | Within 5s SLO |
|---------|-------------|-------------|---------------|---------------|
| 1,000   | 42 ms       | 66 ms       | ~329 kB       | yes           |
| 5,000   | 49 ms       | 80 ms       | ~1.56 MB      | yes           |
| 10,000  | 57 ms       | 96 ms       | ~3.13 MB      | yes           |

The seconds-level response requirement is met with wide margin; on a
production Iceberg cluster, partition pruning plus file compaction keeps the
same profile at larger volumes.

## Integration points

- Person 1 → Person 2: queries target Person 1's `audit.*` Iceberg schema;
  the DuckDB backend reads the same Parquet+ZSTD layout.
- Person 2 → Person 3: every service query is logged by the application
  layer (trace id, timestamp, latency) so Person 3 can validate audit-trail
  completeness; consistency report (Q5) feeds Person 3's compliance reports.
- Person 3 → Person 1: retention/deletion requirements translate to Iceberg
  snapshot expiration / TTL policies owned by Person 1.

## Data generation

Monthly batches are synthetic and deterministic:

```bash
python3 person2_query_interface/data/generator.py
```

Two production-snapshot months are generated (2026-10 on model
`loan-risk-demo-v1.0.0`, 2026-11 on `loan-risk-demo-v1.1.0`) plus a
2026-12 batch, demonstrating model iteration across monthly batches.
