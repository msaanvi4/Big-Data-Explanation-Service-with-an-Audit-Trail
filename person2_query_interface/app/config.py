"""Person 2 configuration.

Runtime knobs for the query interface and reproducibility engine.
All paths are relative to this repository root.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # .../Big-Data-Explanation-Service-with-an-Audit-Trail
PERSON2_DIR = REPO_ROOT / "person2_query_interface"
DATA_DIR = PERSON2_DIR / "data"
MONTHLY_BATCHES_DIR = DATA_DIR / "monthly_batches"
FIXTURE_PATH = DATA_DIR / "synthetic_audit_records.json"
SQL_DIR = PERSON2_DIR / "sql"

# Backend selection: "duckdb" (Parquet files, runs without a cluster),
# "fixture" (JSON fixture only), or "iceberg" (real Apache Iceberg catalog
# via pyiceberg). On Iceberg the same SQL in sql/queries.sql runs with native
# VERSION AS OF time travel.
REPO_BACKEND = os.getenv("EXPLANATION_REPO_BACKEND", "duckdb")

# Iceberg catalog settings
ICEBERG_CATALOG_NAME = os.getenv("ICEBERG_CATALOG_NAME", "demo")
ICEBERG_NAMESPACE = os.getenv("ICEBERG_NAMESPACE", "audit")
ICEBERG_WAREHOUSE = os.getenv(
    "ICEBERG_WAREHOUSE",
    str(REPO_ROOT / "iceberg_warehouse"),
)

# Reproducibility tolerances
SCORE_TOLERANCE = float(os.getenv("EXPLANATION_SCORE_TOLERANCE", "1e-6"))  # exact score match
DECISION_TOLERANCE = 0.0  # decision must match exactly

# Demo decision thresholds (threshold_policy_version = demo-thresholds-v1)
THRESHOLD_APPROVE = 0.40  # risk_score < 0.40  -> APPROVE
THRESHOLD_REVIEW_MAX = 0.70  # 0.40 <= score < 0.70 -> REVIEW
# score >= 0.70 -> DECLINE

# Explainer registry: method name -> {versions: [..], default_version}
EXPLAINER_REGISTRY = {
    "feature_importance": {"versions": ["1.0.0"], "default_version": "1.0.0"},
    "shap": {"versions": ["1.0.0"], "default_version": "1.0.0"},
    "lime": {"versions": ["1.0.0"], "default_version": "1.0.0"},
}

# Model versions pinned in this demo. Each is an immutable artifact with its
# own deterministic weights and threshold policy.
PINNED_MODEL_VERSIONS = ["loan-risk-demo-v1.0.0", "loan-risk-demo-v1.1.0"]

DEFAULT_MODEL_VERSION = "loan-risk-demo-v1.0.0"
DEFAULT_SCHEMA_VERSION = "1.0.0"

# Performance SLO: the explain lookup must answer within this many seconds.
QUERY_SLO_SECONDS = 5.0
