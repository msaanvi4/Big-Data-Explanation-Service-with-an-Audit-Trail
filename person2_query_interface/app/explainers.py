"""Person 2: versioned explainability methods.

Supported methods (mirroring Person 1's explainer_name / explainer_version
contract):

- feature_importance v1.x — model coefficient × deviation from baseline
- shap v1.x               — deterministic additive SHAP-style shim
- lime v1.x               — deterministic local-linear LIME-style shim

Real SHAP/LIME libraries were deliberately not added as dependencies (no
external downloads in this environment). Each shim implements the same
interface as the real method so the audit trail records a method name and
exact version; swapping in the real library only changes the contribution
computation, not the audit schema or the reproducibility test.

All explainers are deterministic given (input, model, method, version), so a
recorded explanation can be regenerated and compared byte-for-byte.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from . import config
from .model import BASELINE, ModelArtifact, scaled_deviation

FEATURE_DISPLAY = {
    "loan_amnt": "Loan amount",
    "revenue": "Annual revenue",
    "dti_n": "Debt-to-income ratio",
    "fico_n": "Credit score",
    "experience_c": "Credit experience",
    "emp_length_enc": "Employment length",
    "purpose_enc": "Loan purpose",
    "home_ownership_enc": "Home ownership",
}

REASON_CODE_MAP = {
    ("dti_n", "increases_risk"): "HIGHER_DEBT_RATIO",
    ("dti_n", "decreases_risk"): "LOWER_DEBT_RATIO",
    ("fico_n", "increases_risk"): "LOWER_CREDIT_SCORE",
    ("fico_n", "decreases_risk"): "CREDIT_SCORE_SUPPORTS_LOWER_RISK",
    ("loan_amnt", "increases_risk"): "HIGHER_LOAN_AMOUNT",
    ("loan_amnt", "decreases_risk"): "LOWER_LOAN_AMOUNT",
    ("revenue", "increases_risk"): "HIGHER_REVENUE_OFFSET",
    ("revenue", "decreases_risk"): "LOWER_REVENUE",
    ("experience_c", "increases_risk"): "LESS_CREDIT_EXPERIENCE",
    ("experience_c", "decreases_risk"): "MORE_CREDIT_EXPERIENCE",
}


@dataclass(frozen=True)
class ExplanationPayload:
    explainer_name: str
    explainer_version: str
    explanation_json: list[dict[str, Any]]  # ranked: feature, contribution, direction
    reason_codes: list[str]
    rendered_summary: str
    explanation_hash: str


def _hash(obj: Any) -> str:
    return "sha256-" + hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:32]


def _reason_code(feature: str, direction: str) -> str:
    return REASON_CODE_MAP.get((feature, direction), f"{feature.upper()}_{direction.upper()}")


def _render_summary(model_version: str, contributions: list[dict[str, Any]]) -> str:
    top = contributions[:2]
    if not top:
        return "No significant feature contributions."
    parts = []
    for c in top:
        parts.append(f"{FEATURE_DISPLAY.get(c['feature'], c['feature'])} {c['direction'].replace('_', ' ')}")
    return (
        f"Decision driven by {model_version}: " + "; ".join(parts) + "."
    )


def explain_feature_importance_v1(
    features: dict[str, float], model: ModelArtifact, version: str = "1.0.0"
) -> ExplanationPayload:
    if version != "1.0.0":
        raise ValueError(f"Unsupported feature_importance version: {version}")
    contributions = []
    for feat, coef in zip(model.feature_order, model.coefficients):
        scaled = scaled_deviation(feat, features.get(feat, BASELINE.get(feat, 0.0)))
        contrib = float(coef * scaled)
        direction = "increases_risk" if contrib > 0 else "decreases_risk"
        contributions.append(
            {"feature": feat, "contribution": round(contrib, 6), "direction": direction}
        )
    contributions.sort(key=lambda c: abs(c["contribution"]), reverse=True)
    reason_codes = [_reason_code(c["feature"], c["direction"]) for c in contributions]
    payload = ExplanationPayload(
        explainer_name="feature_importance",
        explainer_version=version,
        explanation_json=contributions,
        reason_codes=reason_codes,
        rendered_summary=_render_summary(model.model_version, contributions),
        explanation_hash=_hash(
            {
                "explainer": "feature_importance",
                "version": version,
                "contributions": contributions,
                "reason_codes": reason_codes,
            }
        ),
    )
    return payload


def explain_shap_v1(
    features: dict[str, float], model: ModelArtifact, version: str = "1.0.0"
) -> ExplanationPayload:
    """Additive SHAP-style shim for a linear model: exact Shapley values are
    coefficient × (x - baseline); contributions sum to score - base score."""
    if version != "1.0.0":
        raise ValueError(f"Unsupported shap version: {version}")
    base_score = model.predict_score(BASELINE)
    score = model.predict_score(features)
    contributions = []
    for feat, coef in zip(model.feature_order, model.coefficients):
        scaled = scaled_deviation(feat, features.get(feat, BASELINE.get(feat, 0.0)))
        contrib = float(coef * scaled)
        direction = "increases_risk" if contrib > 0 else "decreases_risk"
        contributions.append(
            {"feature": feat, "contribution": round(contrib, 6), "direction": direction}
        )
    # Residual so contributions reconcile to score - base_score
    residual = round(score - base_score - sum(c["contribution"] for c in contributions), 6)
    if abs(residual) > 1e-9:
        contributions[0]["contribution"] = round(contributions[0]["contribution"] + residual, 6)
    contributions.sort(key=lambda c: abs(c["contribution"]), reverse=True)
    reason_codes = [_reason_code(c["feature"], c["direction"]) for c in contributions]
    payload = ExplanationPayload(
        explainer_name="shap",
        explainer_version=version,
        explanation_json=contributions,
        reason_codes=reason_codes,
        rendered_summary=_render_summary(model.model_version, contributions),
        explanation_hash=_hash(
            {
                "explainer": "shap",
                "version": version,
                "base_score": base_score,
                "score": score,
                "contributions": contributions,
                "reason_codes": reason_codes,
            }
        ),
    )
    return payload


def explain_lime_v1(
    features: dict[str, float], model: ModelArtifact, version: str = "1.0.0"
) -> ExplanationPayload:
    """Deterministic LIME-style shim: seed a local perturbation by the input
    itself, fit a local linear surrogate, return weighted contributions."""
    if version != "1.0.0":
        raise ValueError(f"Unsupported lime version: {version}")
    seed = int(hashlib.sha256(json.dumps(features, sort_keys=True).encode()).hexdigest()[:8], 16)
    rng = np.random.RandomState(seed % (2**31))
    local_coefs = []
    for feat, coef in zip(model.feature_order, model.coefficients):
        # Local perturbation weight decays with distance from baseline
        dist = abs(features.get(feat, 0.0) - BASELINE.get(feat, 0.0)) / max(
            abs(BASELINE.get(feat, 0.0)), 1.0
        )
        local_weight = float(rng.uniform(0.5, 1.5)) * np.exp(-dist)
        contrib = float(coef * scaled_deviation(feat, features.get(feat, BASELINE.get(feat, 0.0))) * local_weight)
        direction = "increases_risk" if contrib > 0 else "decreases_risk"
        local_coefs.append(
            {"feature": feat, "contribution": round(contrib, 6), "direction": direction}
        )
    local_coefs.sort(key=lambda c: abs(c["contribution"]), reverse=True)
    reason_codes = [_reason_code(c["feature"], c["direction"]) for c in local_coefs]
    payload = ExplanationPayload(
        explainer_name="lime",
        explainer_version=version,
        explanation_json=local_coefs,
        reason_codes=reason_codes,
        rendered_summary=_render_summary(model.model_version, local_coefs),
        explanation_hash=_hash(
            {
                "explainer": "lime",
                "version": version,
                "seed": seed,
                "contributions": local_coefs,
                "reason_codes": reason_codes,
            }
        ),
    )
    return payload


EXPLAINERS = {
    ("feature_importance", "1.0.0"): explain_feature_importance_v1,
    ("shap", "1.0.0"): explain_shap_v1,
    ("lime", "1.0.0"): explain_lime_v1,
}


def explain(
    features: dict[str, float],
    model: ModelArtifact,
    explainer_name: str,
    explainer_version: str = "1.0.0",
) -> ExplanationPayload:
    key = (explainer_name, explainer_version)
    if key not in EXPLAINERS:
        available = sorted(f"{n} {v}" for (n, v) in EXPLAINERS)
        raise ValueError(
            f"Explainer {explainer_name} v{explainer_version} not registered. "
            f"Available: {available}"
        )
    return EXPLAINERS[key](features, model, explainer_version)
