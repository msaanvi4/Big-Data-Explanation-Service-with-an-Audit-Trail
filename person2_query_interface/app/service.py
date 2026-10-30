"""Person 2: query interface application layer (FastAPI).

Endpoints:

* GET /explain?request_trace_id=...&decision_ts=...
  "Why was this decision made?" — returns decision, score, model version,
  input snapshot, and explanation for a customer reference within a time
  window (Q1).
* GET /decision/{prediction_id} — full audit bundle (Q2).
* GET /decision/{prediction_id}/at/{snapshot_id} — point-in-time bundle via
  Iceberg time-travel emulation (Q3).
* GET /replay/{prediction_id} — reproducibility replay: re-run the pinned
  model on the stored input and validate the explanation against the audit
  trail.
* GET /consistency — decision consistency report for compliance
  (same inputs always same decision?).
* GET /health — service health.

The service is backend-agnostic: DuckDB/Parquet by default, JSON fixture as
fallback, Spark/Iceberg in production (same SQL in sql/queries.sql).
"""

from __future__ import annotations

from datetime import datetime
from time import perf_counter
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import config
from .explainers import explain
from .model import ModelRegistry, demo_decision
from .repository import (
    AuditRepository,
    DecisionBundle,
    NotFoundError,
    get_repository,
)
from .reproducibility import ReproducibilityEngine

app = FastAPI(
    title="Explanation Service with Audit Trail — Person 2",
    description="Query interface and reproducibility engine over Person 1's "
    "Iceberg audit schema (Parquet + ZSTD, time-partitioned).",
    version="1.0.0",
)

_repository: AuditRepository | None = None
_engine: ReproducibilityEngine | None = None


def get_repo() -> AuditRepository:
    global _repository
    if _repository is None:
        _repository = get_repository()
    return _repository


def get_engine() -> ReproducibilityEngine:
    global _engine
    if _engine is None:
        _engine = ReproducibilityEngine(get_repo())
    return _engine


# --------------------------------------------------------------------------- #
# Response helpers
# --------------------------------------------------------------------------- #
def _bundle_to_dict(bundle: DecisionBundle) -> dict[str, Any]:
    e, i, x, m = bundle.prediction, bundle.input, bundle.explanation, bundle.model
    return {
        "prediction": {
            "prediction_id": e.prediction_id,
            "decision_ts": e.decision_ts,
            "decision_date": e.decision_date,
            "demo_decision": e.demo_decision,
            "risk_score": e.risk_score,
            "model_version": e.model_version,
            "input_snapshot_id": e.input_snapshot_id,
            "explanation_id": e.explanation_id,
            "request_trace_id": e.request_trace_id,
            "schema_version": e.schema_version,
        },
        "input_snapshot": {
            "input_snapshot_id": i.input_snapshot_id,
            "feature_schema_version": i.feature_schema_version,
            "loan_amnt": i.loan_amnt,
            "revenue": i.revenue,
            "dti_n": i.dti_n,
            "fico_n": i.fico_n,
            "experience_c": i.experience_c,
            "emp_length": i.emp_length,
            "purpose": i.purpose,
            "home_ownership_n": i.home_ownership_n,
            "input_hash": i.input_hash,
            "created_at": i.created_at,
        },
        "explanation": {
            "explanation_id": x.explanation_id,
            "explainer_name": x.explainer_name,
            "explainer_version": x.explainer_version,
            "explanation_json": x.explanation_json,
            "reason_codes": x.reason_codes,
            "rendered_summary": x.rendered_summary,
            "explanation_hash": x.explanation_hash,
            "created_at": x.created_at,
        },
        "model": {
            "model_version": m.model_version,
            "artifact_uri": m.artifact_uri,
            "artifact_sha256": m.artifact_sha256,
            "framework": m.framework,
            "runtime_version": m.runtime_version,
            "threshold_policy_version": m.threshold_policy_version,
            "explainer_compatibility": m.explainer_compatibility,
        },
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "backend": config.REPO_BACKEND}


@app.get("/explain")
def explain_decision(
    request_trace_id: str = Query(..., description="Synthetic customer reference (request_trace_id)"),
    from_ts: str = Query("1970-01-01T00:00:00Z", description="Window start (inclusive)"),
    to_ts: str = Query("2100-01-01T00:00:00Z", description="Window end (inclusive)"),
) -> dict[str, Any]:
    """Why was this decision made? Lookup by customer reference + timestamp."""
    start = perf_counter()
    repo = get_repo()
    try:
        event = repo.find_decision_by_trace_and_time(request_trace_id, from_ts, to_ts)
    except Exception as exc:  # pragma: no cover - backend errors surface as 502
        raise HTTPException(status_code=502, detail=f"Query backend error: {exc}")
    if event is None:
        raise HTTPException(
            status_code=404,
            detail=f"No decision found for request_trace_id={request_trace_id} in [{from_ts}, {to_ts}]",
        )

    bundle = repo.get_decision_bundle(event.prediction_id)
    latency_ms = (perf_counter() - start) * 1000
    bytes_scanned = repo.bytes_scanned(
        ("prediction_events", "input_snapshots", "explanation_records", "model_registry"),
        None, None,
    )

    return {
        "query": {
            "request_trace_id": request_trace_id,
            "from_ts": from_ts,
            "to_ts": to_ts,
            "resolved_prediction_id": event.prediction_id,
        },
        "decision": _bundle_to_dict(bundle),
        "observability": {
            "latency_ms": round(latency_ms, 3),
            "bytes_scanned": bytes_scanned,
            "slo_seconds": config.QUERY_SLO_SECONDS,
            "within_slo": latency_ms / 1000.0 <= config.QUERY_SLO_SECONDS,
        },
    }


@app.get("/decision/{prediction_id}")
def get_decision(prediction_id: str) -> dict[str, Any]:
    repo = get_repo()
    try:
        bundle = repo.get_decision_bundle(prediction_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail=f"Prediction not found: {prediction_id}")
    return _bundle_to_dict(bundle)


@app.get("/decision/{prediction_id}/at/{snapshot_id}")
def get_decision_at_snapshot(prediction_id: str, snapshot_id: str) -> dict[str, Any]:
    """Point-in-time decision (Iceberg time travel: VERSION AS OF equivalent)."""
    repo = get_repo()
    try:
        bundle = repo.get_decision_bundle_at_snapshot(prediction_id, snapshot_id)
    except NotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Prediction {prediction_id} not found at snapshot {snapshot_id}",
        )
    return {
        "snapshot_id": snapshot_id,
        "decision": _bundle_to_dict(bundle),
    }


@app.get("/replay/{prediction_id}")
def replay_prediction(prediction_id: str) -> dict[str, Any]:
    """Re-run the pinned model on the stored input and validate against the audit trail."""
    engine = get_engine()
    try:
        result = engine.replay_prediction(prediction_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail=f"Prediction not found: {prediction_id}")
    return {
        "prediction_id": result.prediction_id,
        "reproducible": result.replayed,
        "score_match": result.score_match,
        "decision_match": result.decision_match,
        "explanation_match": result.explanation_match,
        "input_hash_verified": result.input_hash_verified,
        "explanation_hash_verified": result.explanation_hash_verified,
        "original_score": result.original_score,
        "replayed_score": result.replayed_score,
        "score_diff": result.score_diff,
        "tolerance": result.tolerance,
        "original_decision": result.original_decision,
        "replayed_decision": result.replayed_decision,
        "model_version": result.model_version,
        "explainer_name": result.explainer_name,
        "explainer_version": result.explainer_version,
        "details": result.details,
    }


@app.get("/consistency")
def consistency_report() -> dict[str, Any]:
    """Compliance view: did the same customer + same inputs always get the
    same decision across model versions and time?"""
    engine = get_engine()
    return engine.consistency_report()
