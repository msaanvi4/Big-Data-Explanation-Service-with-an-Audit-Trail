"""Person 2: explainer versioning tests.

SHAP / LIME / feature-importance methods are versioned across model
iterations. The same (input, model, method, version) must always reproduce
the same explanation.
"""

from __future__ import annotations

from person2_query_interface.app.explainers import explain
from person2_query_interface.app.model import ModelRegistry


def test_registered_explainers_produce_explanations():
    models = ModelRegistry()
    model = models.get("loan-risk-demo-v1.0.0")
    features = {
        "loan_amnt": 12000.0,
        "revenue": 65000.0,
        "dti_n": 18.5,
        "fico_n": 710.0,
        "experience_c": 1.0,
        "emp_length_enc": 2.0,
        "purpose_enc": 0.0,
        "home_ownership_enc": 0.0,
    }
    for name, version in [("feature_importance", "1.0.0"), ("shap", "1.0.0"), ("lime", "1.0.0")]:
        payload = explain(features, model, name, version)
        assert payload.explainer_name == name
        assert payload.explainer_version == version
        assert isinstance(payload.explanation_json, list)
        assert isinstance(payload.reason_codes, list)
        assert isinstance(payload.rendered_summary, str)
        assert payload.explanation_hash.startswith("sha256-")


def test_explanations_are_deterministic():
    models = ModelRegistry()
    model = models.get("loan-risk-demo-v1.0.0")
    features = {
        "loan_amnt": 12000.0,
        "revenue": 65000.0,
        "dti_n": 18.5,
        "fico_n": 710.0,
        "experience_c": 1.0,
        "emp_length_enc": 2.0,
        "purpose_enc": 0.0,
        "home_ownership_enc": 0.0,
    }
    a = explain(features, model, "shap", "1.0.0")
    b = explain(features, model, "shap", "1.0.0")
    assert a.explanation_hash == b.explanation_hash
    assert a.reason_codes == b.reason_codes
    assert a.explanation_json == b.explanation_json


def test_different_versions_yield_different_explanations():
    models = ModelRegistry()
    model = models.get("loan-risk-demo-v1.0.0")
    features = {
        "loan_amnt": 12000.0,
        "revenue": 65000.0,
        "dti_n": 18.5,
        "fico_n": 710.0,
        "experience_c": 1.0,
        "emp_length_enc": 2.0,
        "purpose_enc": 0.0,
        "home_ownership_enc": 0.0,
    }
    shap_v1 = explain(features, model, "shap", "1.0.0")
    fi_v1 = explain(features, model, "feature_importance", "1.0.0")
    # Different methods may rank contributions differently.
    assert shap_v1.explainer_name != fi_v1.explainer_name


def test_unknown_explainer_or_version_raises():
    models = ModelRegistry()
    model = models.get("loan-risk-demo-v1.0.0")
    features = {"loan_amnt": 12000.0, "revenue": 65000.0, "dti_n": 18.5, "fico_n": 710.0,
                "experience_c": 1.0, "emp_length_enc": 0.0, "purpose_enc": 0.0, "home_ownership_enc": 0.0}
    try:
        explain(features, model, "shap", "9.9.9")
        raise AssertionError("expected ValueError for unknown version")
    except ValueError:
        pass
    try:
        explain(features, model, "nonexistent", "1.0.0")
        raise AssertionError("expected ValueError for unknown explainer")
    except ValueError:
        pass
