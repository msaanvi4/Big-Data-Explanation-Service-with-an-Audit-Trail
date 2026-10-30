"""Person 2: reproducibility engine.

Given a prediction, the engine retrieves the exact model version, weights,
input snapshot, and explainability method used at prediction time, re-runs the
model on the stored input, and validates that the explanation and decision
match the audit trail. This answers "can this decision be reproduced?" and
supports the regulator's consistency question: same customer + same inputs
always yields the same decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import config
from .explainers import explain
from .model import ModelRegistry, demo_decision
from .repository import AuditRepository, DecisionBundle, NotFoundError


@dataclass
class ReplayResult:
    prediction_id: str
    replayed: bool
    score_match: bool
    decision_match: bool
    explanation_match: bool
    input_hash_verified: bool
    explanation_hash_verified: bool
    original_score: float
    replayed_score: float
    score_diff: float
    original_decision: str
    replayed_decision: str
    model_version: str
    explainer_name: str
    explainer_version: str
    tolerance: float
    details: dict[str, Any]


class ReproducibilityEngine:
    def __init__(self, repository: AuditRepository | None = None) -> None:
        self.repository = repository or config.get_repository()
        self.models = ModelRegistry()

    def replay_prediction(self, prediction_id: str) -> ReplayResult:
        bundle = self.repository.get_decision_bundle(prediction_id)
        return self._replay_bundle(bundle)

    def replay_prediction_at_snapshot(
        self, prediction_id: str, snapshot_id: str
    ) -> ReplayResult:
        bundle = self.repository.get_decision_bundle_at_snapshot(prediction_id, snapshot_id)
        return self._replay_bundle(bundle, point_in_time_snapshot=snapshot_id)

    def replay_all(self) -> list[ReplayResult]:
        return [self.replay_prediction(pid) for pid in self.repository.list_prediction_ids()]

    def _replay_bundle(
        self, bundle: DecisionBundle, point_in_time_snapshot: str | None = None
    ) -> ReplayResult:
        event = bundle.prediction
        snapshot = bundle.input
        explanation = bundle.explanation
        model_record = bundle.model

        model = self.models.get(model_record.model_version)

        # 1. Verify stored input hash against the canonical snapshot hash.
        input_hash_verified = self._verify_input_hash(snapshot)

        # 2. Re-run the pinned model on the stored input snapshot.
        features = snapshot.to_model_features()
        replayed_score = model.predict_score(features)
        original_score = event.risk_score
        score_diff = abs(replayed_score - original_score)
        score_match = score_diff <= config.SCORE_TOLERANCE

        # 3. Decision must match exactly under the recorded threshold policy.
        replayed_decision = demo_decision(
            replayed_score, model_record.threshold_policy_version
        )
        original_decision = event.demo_decision
        decision_match = replayed_decision == original_decision

        # 4. Regenerate the explanation with the recorded explainer version
        #    and compare reason codes + ranked contribution order to the audit trail.
        replayed_explanation = explain(
            features, model, explanation.explainer_name, explanation.explainer_version
        )
        reason_codes_match = replayed_explanation.reason_codes == explanation.reason_codes
        ranking_match = [
            c["feature"] for c in replayed_explanation.explanation_json
        ] == [c["feature"] for c in explanation.explanation_json]
        explanation_match = reason_codes_match and ranking_match

        # 5. Verify stored explanation hash is reproducible.
        explanation_hash_verified = (
            replayed_explanation.explanation_hash == explanation.explanation_hash
        )

        replayed = score_match and decision_match and explanation_match

        details: dict[str, Any] = {
            "input_snapshot_id": snapshot.input_snapshot_id,
            "feature_schema_version": snapshot.feature_schema_version,
            "threshold_policy_version": model_record.threshold_policy_version,
            "reason_codes_match": reason_codes_match,
            "contribution_ranking_match": ranking_match,
            "point_in_time_snapshot": point_in_time_snapshot,
        }

        return ReplayResult(
            prediction_id=event.prediction_id,
            replayed=replayed,
            score_match=score_match,
            decision_match=decision_match,
            explanation_match=explanation_match,
            input_hash_verified=input_hash_verified,
            explanation_hash_verified=explanation_hash_verified,
            original_score=original_score,
            replayed_score=replayed_score,
            score_diff=score_diff,
            original_decision=original_decision,
            replayed_decision=replayed_decision,
            model_version=model.model_version,
            explainer_name=explanation.explainer_name,
            explainer_version=explanation.explainer_version,
            tolerance=config.SCORE_TOLERANCE,
            details=details,
        )

    @staticmethod
    def _verify_input_hash(snapshot) -> bool:
        """Person 1's contract: input_hash is the SHA-256 of canonical sorted
        JSON of the model inputs. Recompute and compare."""
        import hashlib
        import json

        canonical = json.dumps(
            {
                "loan_amnt": snapshot.loan_amnt,
                "revenue": snapshot.revenue,
                "dti_n": snapshot.dti_n,
                "fico_n": snapshot.fico_n,
                "experience_c": snapshot.experience_c,
                "emp_length": snapshot.emp_length,
                "purpose": snapshot.purpose,
                "home_ownership_n": snapshot.home_ownership_n,
            },
            sort_keys=True,
        )
        digest = "sha256-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
        return digest == snapshot.input_hash

    def consistency_report(self) -> dict[str, Any]:
        """Same inputs -> same decision across model versions and time.

        Returns violations (input_hash with >1 distinct decision) and a
        pass/fail flag for the compliance report.
        """
        violations = self.repository.consistency_violations()
        return {
            "consistent": len(violations) == 0,
            "violation_count": len(violations),
            "violations": violations,
        }
