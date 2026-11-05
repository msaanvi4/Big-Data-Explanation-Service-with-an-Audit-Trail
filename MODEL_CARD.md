# Model Card — Loan-Risk Demo Models

**Project:** Explanation Service with an Audit Trail  
**Owner:** Person 2 (Query Interface & Reproducibility), building on Person 1's storage design  
**Version:** 1.0.0  
**Date:** 2026-11-05  
**Status:** Demonstration / course project (synthetic data only)

---

## 1. Model details

| Field | Value |
|-------|-------|
| Model name | loan-risk-demo |
| Model versions | `loan-risk-demo-v1.0.0`, `loan-risk-demo-v1.1.0` |
| Model type | Deterministic logistic regression demo (sigmoid over scaled deviation-from-baseline features) |
| Framework | deterministic-logistic-demo (declared runtime: python-3.12-scikit-learn-1.9.1) |
| Task | Binary-style risk scoring → demo decision (APPROVE / REVIEW / DECLINE) |
| License | N/A — synthetic, internal demonstration artifact |

### Intended use
To demonstrate an auditable, reproducible explanation service: given a stored model input snapshot, reproduce the historical risk score, demo decision, and explanation exactly. **Not for real lending decisions.** All outputs are synthetic demonstrations.

### Threshold policy (`demo-thresholds-v1`)
- risk_score < 0.40 → APPROVE
- 0.40 ≤ risk_score < 0.70 → REVIEW
- risk_score ≥ 0.70 → DECLINE

---

## 2. Factors

### Features (14, canonical model input)
| Feature | Description | Source |
|---------|-------------|--------|
| income_total | Total annual income | Home Credit AMT_INCOME_TOTAL |
| credit_amount | Credit amount | Home Credit AMT_CREDIT |
| annuity | Monthly annuity | Home Credit AMT_ANNUITY |
| dti_ratio | annuity / income_total | Derived |
| credit_score_proxy | 300–850 proxy from EXT_SOURCE_1/2/3 | Derived |
| days_birth | Age in days (negative from application date) | Home Credit DAYS_BIRTH |
| days_employed | Employment duration in days (365243 → 0) | Home Credit DAYS_EMPLOYED |
| gender_enc | Gender index (M/F/XNA) | Home Credit CODE_GENDER |
| owns_car_enc | Car ownership flag | Home Credit FLAG_OWN_CAR |
| owns_realty_enc | Realty ownership flag | Home Credit FLAG_OWN_REALTY |
| children_count | Number of children | Home Credit CNT_CHILDREN |
| education_enc | Education-type index | Home Credit NAME_EDUCATION_TYPE |
| housing_enc | Housing-type index | Home Credit NAME_HOUSING_TYPE |
| occupation_enc | Occupation-type index | Home Credit OCCUPATION_TYPE |

### Version differences
`v1.0.0` (Oct-2026 batch) and `v1.1.0` (Nov/Dec-2026 batches) use different deterministic coefficients derived from a version-keyed seed, simulating model recalibration across monthly batches. Income and credit-score coefficients are constrained negative (risk-reducing); credit amount, annuity, and DTI are constrained positive (risk-increasing).

---

## 3. Metrics

Reproducibility is the primary quality metric, validated by `ReproducibilityEngine.replay_prediction`:

| Metric | Criterion | Result |
|--------|-----------|--------|
| Score match | \|replayed − stored\| ≤ 1e-6 | Pass |
| Decision match | replayed demo_decision == stored demo_decision | Pass (exact) |
| Input hash | SHA-256(canonical sorted inputs) matches stored input_hash | Pass |
| Explanation match | reason codes + contribution ranking match stored explanation | Pass |
| Explanation hash | regenerated explanation_hash == stored explanation_hash | Pass |

Holds across all monthly batches (2026-10 / 2026-11 / 2026-12) and all three explainer methods (feature_importance, shap, lime at 1.0.0).

---

## 4. Training data

Curated 15-feature subset of the Home Credit Default Risk training set (`application_train.csv`, 307,511 rows). `SK_ID_CURR` is read only transiently to derive deterministic UUID5s and is never stored. Missing EXT_SOURCE values are median-imputed; the unemployed sentinel 365243 is mapped to 0. `TARGET` (final repayment outcome) is not used as an accept/deny label; decisions are synthetic demo decisions.

---

## 5. Evaluation

- **Person 2 pytest:** 27 tests passing, including replay of deterministic samples across all monthly batches (exact score, exact decision, hash verification).
- **Person 3 compliance:** 39 unit + 5 integration tests passing; final compliance report `COMPLIANT` on 3,000 synthetic predictions, 0 consistency violations.
- **Benchmarks:** p95 ≤ ~100 ms at 10,000 records, within the 5-second SLO.

---

## 6. Ethical considerations

- No real borrower data is stored: `SK_ID_CURR`, raw ZIP, free text, and loan IDs are excluded from audit tables.
- All IDs are synthetic UUIDs; `request_trace_id` is the customer-facing reference.
- Decisions are explicitly labeled *demonstration* decisions, not real credit outcomes.
- Model cards explain contributions via reason codes from a stable vocabulary, supporting transparency.

---

## 7. Caveats and recommendations

- Deterministic shims stand in for real SHAP/LIME; swap in production libraries when available, preserving the audit schema.
- Artifact URIs (`s3://example-bucket/...`) are placeholders; a production deployment stores binaries in immutable object storage and verifies `artifact_sha256` before deserialization.
- Not validated on real applicant populations; performance and fairness on real data are unknown.
