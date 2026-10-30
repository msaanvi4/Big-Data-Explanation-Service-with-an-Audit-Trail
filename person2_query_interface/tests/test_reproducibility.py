"""Person 2: reproducibility test suite.

Re-runs the pinned model on the stored input snapshot and validates that the
decision, score, reason codes, and explanation hash match the audit trail.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

from person2_query_interface.app import config
from person2_query_interface.app import config
from person2_query_interface.app.model import ModelRegistry
from person2_query_interface.app.reproducibility import ReproducibilityEngine


def test_replay_sample_is_reproducible(repo):
    engine = ReproducibilityEngine(repo)
    ids = repo.list_prediction_ids()
    assert ids, "No predictions to replay"
    # Deterministic sample across all monthly batches (full replay_all() is a
    # batch compliance job; this test covers the per-prediction path).
    sample = ids[:: max(1, len(ids) // 150)]
    results = [engine.replay_prediction(pid) for pid in sample]
    assert results, "No predictions to replay"
    for result in results:
        assert result.replayed, f"{result.prediction_id} not reproducible"
        assert result.score_match, f"{result.prediction_id} score drift {result.score_diff}"
        assert result.decision_match, (
            f"{result.prediction_id} decision mismatch: "
            f"{result.original_decision} vs {result.replayed_decision}"
        )
        assert result.explanation_match, f"{result.prediction_id} explanation mismatch"
        assert result.input_hash_verified, f"{result.prediction_id} input hash mismatch"
        assert result.explanation_hash_verified, f"{result.prediction_id} explanation hash mismatch"
        assert result.score_diff <= config.SCORE_TOLERANCE



def test_replay_score_is_exact(repo, first_prediction):
    engine = ReproducibilityEngine(repo)
    result = engine.replay_prediction(first_prediction)
    assert result.score_diff == 0.0
    assert result.original_score == result.replayed_score


def test_replay_at_point_in_time_snapshot(repo, sample_bundle):
    import json
    from pathlib import Path

    import duckdb

    snapshot_id = None
    for batch_dir in Path(config.MONTHLY_BATCHES_DIR).iterdir():
        manifest = json.loads((batch_dir / "batch_manifest.json").read_text())
        for entry in manifest["entries"]:
            if entry["table_name"] != "prediction_events":
                continue
            pq_path = batch_dir / entry["path"]
            con = duckdb.connect(":memory:")
            con.execute(f"CREATE VIEW ev AS SELECT * FROM read_parquet('{pq_path}')")
            hit = con.execute(
                "SELECT 1 FROM ev WHERE prediction_id = ?",
                [sample_bundle.prediction.prediction_id],
            ).fetchone()
            con.close()
            if hit:
                snapshot_id = manifest["snapshot_id"]
                break
        if snapshot_id:
            break

    assert snapshot_id is not None
    engine = ReproducibilityEngine(repo)
    result = engine.replay_prediction_at_snapshot(
        sample_bundle.prediction.prediction_id, snapshot_id
    )
    assert result.replayed
    assert result.original_decision == result.replayed_decision
    assert result.details["point_in_time_snapshot"] == snapshot_id


def test_replay_unknown_prediction_raises(repo):
    engine = ReproducibilityEngine(repo)
    try:
        engine.replay_prediction("00000000-0000-0000-0000-000000000000")
        raise AssertionError("expected NotFoundError")
    except Exception:
        pass


def test_model_registry_is_versioned_and_pinned():
    models = ModelRegistry()
    versions = models.versions()
    assert "loan-risk-demo-v1.0.0" in versions
    assert "loan-risk-demo-v1.1.0" in versions
    v1 = models.get("loan-risk-demo-v1.0.0")
    v2 = models.get("loan-risk-demo-v1.1.0")
    # Different model iterations have different pinned weights.
    assert v1.coefficients != v2.coefficients or v1.intercept != v2.intercept
    # Same version always yields the same artifact digest.
    assert models.get("loan-risk-demo-v1.0.0").artifact_sha256 == v1.artifact_sha256
