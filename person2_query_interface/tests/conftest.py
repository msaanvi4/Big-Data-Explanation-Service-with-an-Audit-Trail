"""Person 2 test fixtures.

Sets the DuckDB/Parquet backend, generates deterministic synthetic monthly
batches mirroring Person 1's Iceberg schema, and provides a repository
fixture. All data is synthetic (no real borrower values).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("EXPLANATION_REPO_BACKEND", "duckdb")

ROOT = Path(__file__).resolve().parents[2]  # repository root
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from person2_query_interface.data import generator  # noqa: E402
from person2_query_interface.app import config  # noqa: E402

BATCHES = [
    ("2026-10", 500, 20261001, "loan-risk-demo-v1.0.0"),
    ("2026-11", 500, 20261101, "loan-risk-demo-v1.1.0"),
    ("2026-12", 2000, 20261201, "loan-risk-demo-v1.0.0"),
]


@pytest.fixture(scope="session", autouse=True)
def synthetic_monthly_batches():
    """Generate immutable monthly Parquet batches once per test session."""
    for batch_month, record_count, seed, model_version in BATCHES:
        batch = generator.generate_batch(batch_month, record_count, seed, model_version)
        generator.write_parquet(batch, config.MONTHLY_BATCHES_DIR / batch_month)


@pytest.fixture
def repo():
    from person2_query_interface.app.repository import get_repository

    return get_repository()


@pytest.fixture
def first_prediction(repo):
    ids = repo.list_prediction_ids()
    assert ids, "No predictions in synthetic batches"
    return ids[0]


@pytest.fixture
def sample_bundle(repo, first_prediction):
    return repo.get_decision_bundle(first_prediction)
