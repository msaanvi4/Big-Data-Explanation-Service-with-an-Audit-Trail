"""Person 2: query interface tests.

Covers the "why was this decision made?" lookup (Q1/Q2), point-in-time
reads (Q3), audit completeness (Q6), and the Person 1 privacy boundary
(no customer IDs / direct identifiers in audit responses).
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

from person2_query_interface.app import config
from person2_query_interface.app.repository import NotFoundError


def test_find_decision_by_trace_and_time(repo, first_prediction):
    bundle = repo.get_decision_bundle(first_prediction)
    event = bundle.prediction

    found = repo.find_decision_by_trace_and_time(
        event.request_trace_id,
        "1970-01-01T00:00:00Z",
        "2100-01-01T00:00:00Z",
    )
    assert found is not None
    assert found.prediction_id == event.prediction_id
    assert found.demo_decision in {"APPROVE", "REVIEW", "DECLINE"}
    assert 0.0 <= found.risk_score <= 1.0


def test_find_decision_outside_window_returns_none(repo, first_prediction):
    bundle = repo.get_decision_bundle(first_prediction)
    event = bundle.prediction
    # Narrow window that excludes the event's timestamp.
    assert repo.find_decision_by_trace_and_time(
        event.request_trace_id, "1900-01-01T00:00:00Z", "1901-01-01T00:00:00Z"
    ) is None


def test_get_decision_bundle_links_all_artifacts(repo, sample_bundle):
    b = sample_bundle
    # Every prediction resolves to exactly one input, explanation, model.
    assert b.input.prediction_id == b.prediction.prediction_id
    assert b.input.input_snapshot_id == b.prediction.input_snapshot_id
    assert b.explanation.prediction_id == b.prediction.prediction_id
    assert b.explanation.explanation_id == b.prediction.explanation_id
    assert b.model.model_version == b.prediction.model_version


def test_get_decision_bundle_contains_explainer_versioning(repo, sample_bundle):
    b = sample_bundle
    assert b.explanation.explainer_name in {"feature_importance", "shap", "lime"}
    assert b.explanation.explainer_version == "1.0.0"
    assert isinstance(b.explanation.reason_codes, list) and b.explanation.reason_codes
    assert len(b.explanation.explanation_json) == len(b.explanation.reason_codes)


def test_point_in_time_read_matches_latest(repo, sample_bundle):
    # Find the snapshot (batch) this prediction was written in.
    import json
    from pathlib import Path

    month_dir = None
    for batch_dir in Path(config.MONTHLY_BATCHES_DIR).iterdir():
        manifest = json.loads((batch_dir / "batch_manifest.json").read_text())
        for entry in manifest["entries"]:
            if entry["table_name"] == "prediction_events":
                pq_path = batch_dir / entry["path"]
                # cheap check: this batch holds the prediction
                import duckdb

                con = duckdb.connect(":memory:")
                con.execute(f"CREATE VIEW ev AS SELECT * FROM read_parquet('{pq_path}')")
                hit = con.execute(
                    "SELECT 1 FROM ev WHERE prediction_id = ?",
                    [sample_bundle.prediction.prediction_id],
                ).fetchone()
                con.close()
                if hit:
                    month_dir = batch_dir
                    snapshot_id = manifest["snapshot_id"]
                    break
        if month_dir:
            break

    assert month_dir is not None, "prediction not located in any monthly batch"
    at_snapshot = repo.get_decision_bundle_at_snapshot(
        sample_bundle.prediction.prediction_id, snapshot_id
    )
    assert at_snapshot.prediction.prediction_id == sample_bundle.prediction.prediction_id
    assert at_snapshot.prediction.demo_decision == sample_bundle.prediction.demo_decision
    assert at_snapshot.prediction.risk_score == sample_bundle.prediction.risk_score
    assert at_snapshot.explanation.explanation_hash == sample_bundle.explanation.explanation_hash


def test_get_decision_bundle_unknown_prediction_raises(repo):
    try:
        repo.get_decision_bundle("00000000-0000-0000-0000-000000000000")
        raise AssertionError("expected NotFoundError")
    except NotFoundError:
        pass


def test_audit_completeness_all_ok(repo, first_prediction):
    bundle = repo.get_decision_bundle(first_prediction)
    rows = repo.audit_completeness(bundle.prediction.request_trace_id)
    assert len(rows) == 1
    assert rows[0]["prediction_id"] == first_prediction
    assert rows[0]["input_status"] == "OK"
    assert rows[0]["explanation_status"] == "OK"
    assert rows[0]["model_status"] == "OK"


def test_consistency_violations_empty_for_unique_inputs(repo):
    violations = repo.consistency_violations()
    # Synthetic generator uses one input snapshot per prediction, so there
    # are no duplicate input hashes and hence no consistency violations.
    assert violations == []


def test_privacy_boundary_no_customer_ids(repo, sample_bundle):
    # Person 1 contract: audit tables exclude real customer IDs, raw ZIP,
    # free text, and source loan IDs. request_trace_id is a synthetic token.
    raw_event = sample_bundle.prediction.raw
    for key in raw_event:
        assert "customer" not in key.lower()
        assert key not in {"zip", "title", "desc", "loan_id", "member_id"}
    bundle_dict = {
        "prediction": sample_bundle.prediction,
        "input": sample_bundle.input,
        "explanation": sample_bundle.explanation,
        "model": sample_bundle.model,
    }
    text = json.dumps(bundle_dict, default=str)
    for forbidden in ("customer_id", "ssn", "zip_code", "title", "desc"):
        assert forbidden not in text.lower()
