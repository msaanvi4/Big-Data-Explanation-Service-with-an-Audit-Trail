"""Person 2: FastAPI application-layer tests (endpoints)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from person2_query_interface.app.service import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"


def test_explain_endpoint(first_prediction):
    from person2_query_interface.app.repository import get_repository

    repo = get_repository()
    event = repo.get_decision_bundle(first_prediction).prediction
    r = client.get(
        "/explain",
        params={
            "request_trace_id": event.request_trace_id,
            "from_ts": "1970-01-01T00:00:00Z",
            "to_ts": "2100-01-01T00:00:00Z",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["query"]["resolved_prediction_id"] == first_prediction
    assert body["decision"]["prediction"]["prediction_id"] == first_prediction
    assert body["observability"]["within_slo"] is True
    assert body["observability"]["slo_seconds"] == 5.0
    assert body["observability"]["latency_ms"] >= 0


def test_explain_not_found():
    r = client.get(
        "/explain",
        params={"request_trace_id": "trace-does-not-exist", "from_ts": "1970-01-01T00:00:00Z", "to_ts": "2100-01-01T00:00:00Z"},
    )
    assert r.status_code == 404


def test_get_decision_endpoint(first_prediction):
    r = client.get(f"/decision/{first_prediction}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"prediction", "input_snapshot", "explanation", "model"}
    assert body["prediction"]["prediction_id"] == first_prediction
    assert body["explanation"]["explainer_name"] in {"feature_importance", "shap", "lime"}


def test_get_decision_not_found():
    r = client.get("/decision/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_replay_endpoint(first_prediction):
    r = client.get(f"/replay/{first_prediction}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reproducible"] is True
    assert body["score_match"] is True
    assert body["decision_match"] is True
    assert body["explanation_match"] is True
    assert body["input_hash_verified"] is True
    assert body["explanation_hash_verified"] is True


def test_consistency_endpoint():
    r = client.get("/consistency")
    assert r.status_code == 200
    body = r.json()
    assert "consistent" in body and "violation_count" in body and "violations" in body
