"""Person 2: reproducible, versioned model artifacts.

Each model version is an immutable build: weights, intercept, feature order,
and threshold policy are pinned by version. Prediction is fully deterministic
given (model_version, input snapshot) so a past decision can be replayed
exactly, which is the core of the reproducibility guarantee.

No external model binaries are downloaded: artifacts are generated
deterministically from the version string, mirroring Person 1's
model_registry contract (artifact_uri + artifact_sha256 are recorded digests,
the binary itself lives in immutable object storage).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from . import config

# Canonical baseline (median synthetic applicant) and per-feature scaling so
# that raw financial values are comparable before the linear score. Shared by
# the model (predict_score) and the explainers so both agree on contributions.
BASELINE = {
    "loan_amnt": 12000.0,
    "revenue": 65000.0,
    "dti_n": 18.5,
    "fico_n": 710.0,
    "experience_c": 1.0,
    "emp_length_enc": 0.0,
    "purpose_enc": 0.0,
    "home_ownership_enc": 0.0,
}

FEATURE_SCALES = {
    "loan_amnt": 1.0 / 50000.0,
    "revenue": 1.0 / 200000.0,
    "dti_n": 1.0 / 5.0,
    "fico_n": 1.0 / 100.0,
    "experience_c": 1.0,
    "emp_length_enc": 1.0,
    "purpose_enc": 1.0,
    "home_ownership_enc": 1.0,
}


def scale_feature(feature: str, value: float) -> float:
    return float(value) * FEATURE_SCALES.get(feature, 1.0)


def scaled_deviation(feature: str, value: float) -> float:
    return scale_feature(feature, float(value) - BASELINE.get(feature, 0.0))


@dataclass(frozen=True)
class ModelArtifact:
    model_version: str
    artifact_uri: str
    artifact_sha256: str
    framework: str
    runtime_version: str
    feature_code_commit: str
    preprocessing_sha256: str
    training_dataset_id: str
    training_dataset_version: str
    training_dataset_sha256: str
    training_window_start: str
    training_window_end: str
    label_definition: str
    threshold_policy_version: str
    explainer_compatibility: str
    registered_at: str

    # Model internals (pinned with the build)
    feature_order: list[str] = field(default_factory=list)
    coefficients: list[float] = field(default_factory=list)
    intercept: float = 0.0

    def predict_score(self, features: dict[str, float]) -> float:
        """Logistic risk score in (0, 1), deterministic for this build.

        Features are scaled the same way at training and inference time, so a
        pinned build always reproduces the same score for the same snapshot.
        """
        x = np.array(
            [
                scaled_deviation(f, features.get(f, BASELINE.get(f, 0.0)))
                for f in self.feature_order
            ],
            dtype=float,
        )
        w = np.array(self.coefficients, dtype=float)
        z = float(np.dot(w, x) + self.intercept)
        z = float(np.clip(z, -60.0, 60.0))  # numerical stability for sigmoid
        return float(1.0 / (1.0 + np.exp(-z)))


def _stable_seed(model_version: str) -> int:
    digest = hashlib.sha256(model_version.encode("utf-8")).hexdigest()
    return int(digest[:16], 16) % (2**31)


def _encode_category(value: str, categories: list[str]) -> float:
    """One-hot style encoding: 1.0 for the matching category, 0.0 otherwise."""
    return 1.0 if value in categories else 0.0


def build_model_artifact(model_version: str) -> ModelArtifact:
    """Construct a pinned deterministic artifact for ``model_version``.

    The weights are derived from a seed keyed by the version string, so the
    same version always produces identical weights. Different versions have
    different weights, simulating model iterations across monthly batches.
    """
    rng = np.random.RandomState(_stable_seed(model_version))

    feature_order = [
        "loan_amnt",
        "revenue",
        "dti_n",
        "fico_n",
        "experience_c",
        "emp_length_enc",
        "purpose_enc",
        "home_ownership_enc",
    ]
    # Coefficients in [-1, 1]: DTI and loan size increase risk; FICO and revenue
    # decrease it. Recalibration between versions changes the weights.
    coefficients = [float(v) for v in rng.uniform(-1.0, 1.0, size=len(feature_order))]
    coefficients[feature_order.index("fico_n")] = -abs(coefficients[feature_order.index("fico_n")])
    coefficients[feature_order.index("revenue")] = -abs(coefficients[feature_order.index("revenue")])
    coefficients[feature_order.index("dti_n")] = abs(coefficients[feature_order.index("dti_n")])
    intercept = float(rng.uniform(-1.0, 1.0))

    payload = {
        "model_version": model_version,
        "feature_order": feature_order,
        "coefficients": coefficients,
        "intercept": intercept,
    }
    artifact_sha256 = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    windows = {
        "loan-risk-demo-v1.0.0": ("2026-01-01", "2026-09-30"),
        "loan-risk-demo-v1.1.0": ("2026-04-01", "2026-09-30"),
    }
    start, end = windows.get(model_version, ("2026-01-01", "2026-09-30"))

    return ModelArtifact(
        model_version=model_version,
        artifact_uri=f"s3://example-bucket/models/{model_version}/model.bin",
        artifact_sha256=artifact_sha256,
        framework="deterministic-logistic-demo",
        runtime_version="python-3.14-scikit-learn-1.9.1",
        feature_code_commit=hashlib.sha256(f"feature-code-{model_version}".encode()).hexdigest()[:12],
        preprocessing_sha256=hashlib.sha256(f"preprocessing-{model_version}".encode()).hexdigest()[:16],
        training_dataset_id="synthetic-loan-demo",
        training_dataset_version="1.0.0",
        training_dataset_sha256=hashlib.sha256(f"dataset-{model_version}".encode()).hexdigest()[:16],
        training_window_start=start,
        training_window_end=end,
        label_definition="synthetic repayment outcome for demonstration only",
        threshold_policy_version="demo-thresholds-v1",
        explainer_compatibility=f"demo-explainers-{model_version.split('-v')[-1]}",
        registered_at=f"2026-10-01T10:00:00Z",
        feature_order=feature_order,
        coefficients=coefficients,
        intercept=intercept,
    )


class ModelRegistry:
    """In-memory registry of pinned model builds.

    On a real platform this resolves artifact_uri from object storage and
    verifies artifact_sha256 before deserialization.
    """

    def __init__(self) -> None:
        self._artifacts: dict[str, ModelArtifact] = {}
        for version in config.PINNED_MODEL_VERSIONS:
            self._artifacts[version] = build_model_artifact(version)

    def get(self, model_version: str) -> ModelArtifact:
        if model_version not in self._artifacts:
            raise KeyError(f"Model version not found in registry: {model_version}")
        return self._artifacts[model_version]

    def versions(self) -> list[str]:
        return list(self._artifacts.keys())


def demo_decision(risk_score: float, threshold_policy_version: str = "demo-thresholds-v1") -> str:
    """Map a risk score to a demo decision under the pinned threshold policy."""
    if threshold_policy_version != "demo-thresholds-v1":
        raise ValueError(f"Unknown threshold policy: {threshold_policy_version}")
    if risk_score < config.THRESHOLD_APPROVE:
        return "APPROVE"
    if risk_score < config.THRESHOLD_REVIEW_MAX:
        return "REVIEW"
    return "DECLINE"
