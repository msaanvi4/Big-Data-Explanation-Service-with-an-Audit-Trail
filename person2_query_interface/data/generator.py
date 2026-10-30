"""Person 2: synthetic monthly batch generator.

Produces Parquet batches under data/monthly_batches/YYYY-MM/ that mirror
Person 1's Iceberg schema (prediction_events, input_snapshots,
explanation_records, model_registry), one folder per monthly production
snapshot. Records are fully synthetic (privacy boundary from Person 1) and
deterministic: the same seed yields the same batch.

Explanations are generated with the same versioned explainer code used by
the service (app/explainers.py), so the reproducibility engine can replay a
prediction and get byte-identical scores, decisions, and explanation hashes.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

import sys
from person2_query_interface.app import config
from person2_query_interface.app.explainers import explain
from person2_query_interface.app.model import ModelRegistry, demo_decision

BATCHES_DIR = config.MONTHLY_BATCHES_DIR

EMP_LENGTH_CATEGORIES = ["< 1 year", "1-4 years", "5-9 years", "10+ years"]
PURPOSE_CATEGORIES = [
    "debt_consolidation",
    "home_improvement",
    "business",
    "medical",
    "major_purchase",
]
HOME_OWNERSHIP_CATEGORIES = ["MORTGAGE", "RENT", "OWN"]

EXPLAINERS_BY_INDEX = ["feature_importance", "shap", "lime"]

# Arrow schema matching Person 1's table contracts.
EVENTS_SCHEMA = pa.schema([
    ("prediction_id", pa.string()),
    ("decision_ts", pa.timestamp("us")),
    ("decision_date", pa.date32()),
    ("demo_decision", pa.string()),
    ("risk_score", pa.float64()),
    ("model_version", pa.string()),
    ("input_snapshot_id", pa.string()),
    ("explanation_id", pa.string()),
    ("request_trace_id", pa.string()),
    ("schema_version", pa.string()),
])
INPUT_SNAPSHOTS_SCHEMA = pa.schema([
    ("input_snapshot_id", pa.string()),
    ("prediction_id", pa.string()),
    ("feature_schema_version", pa.string()),
    ("loan_amnt", pa.decimal128(12, 2)),
    ("revenue", pa.decimal128(12, 2)),
    ("dti_n", pa.decimal128(7, 4)),
    ("fico_n", pa.int32()),
    ("experience_c", pa.bool_()),
    ("emp_length", pa.string()),
    ("purpose", pa.string()),
    ("home_ownership_n", pa.string()),
    ("input_hash", pa.string()),
    ("created_at", pa.timestamp("us")),
])
EXPLANATION_SCHEMA = pa.schema([
    ("explanation_id", pa.string()),
    ("prediction_id", pa.string()),
    ("explainer_name", pa.string()),
    ("explainer_version", pa.string()),
    ("explanation_json", pa.string()),  # stored as JSON string per DDL
    ("reason_codes", pa.string()),       # stored as JSON string per DDL
    ("rendered_summary", pa.string()),
    ("explanation_hash", pa.string()),
    ("created_at", pa.timestamp("us")),
])
MODEL_REGISTRY_SCHEMA = pa.schema([
    ("model_version", pa.string()),
    ("artifact_uri", pa.string()),
    ("artifact_sha256", pa.string()),
    ("framework", pa.string()),
    ("runtime_version", pa.string()),
    ("feature_code_commit", pa.string()),
    ("preprocessing_sha256", pa.string()),
    ("training_dataset_id", pa.string()),
    ("training_dataset_version", pa.string()),
    ("training_dataset_sha256", pa.string()),
    ("training_window_start", pa.date32()),
    ("training_window_end", pa.date32()),
    ("label_definition", pa.string()),
    ("threshold_policy_version", pa.string()),
    ("explainer_compatibility", pa.string()),
    ("registered_at", pa.timestamp("us")),
])


def _canonical_input_hash(snapshot_fields: dict[str, Any]) -> str:
    canonical = json.dumps(snapshot_fields, sort_keys=True, default=str)
    return "sha256-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _encode_features(snapshot: dict[str, Any]) -> dict[str, float]:
    return {
        "loan_amnt": float(snapshot["loan_amnt"]),
        "revenue": float(snapshot["revenue"]),
        "dti_n": float(snapshot["dti_n"]),
        "fico_n": float(snapshot["fico_n"]),
        "experience_c": 1.0 if snapshot["experience_c"] else 0.0,
        "emp_length_enc": float(EMP_LENGTH_CATEGORIES.index(snapshot["emp_length"])),
        "purpose_enc": float(PURPOSE_CATEGORIES.index(snapshot["purpose"])),
        "home_ownership_enc": float(HOME_OWNERSHIP_CATEGORIES.index(snapshot["home_ownership_n"])),
    }


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")


def generate_batch(
    batch_month: str,
    record_count: int,
    seed: int,
    model_version: str,
    start_day: int = 1,
) -> dict[str, Any]:
    """Generate one monthly batch of synthetic audit records."""
    rng = np.random.RandomState(seed)
    models = ModelRegistry()
    model = models.get(model_version)

    month_start = datetime.strptime(batch_month, "%Y-%m")
    events, inputs, explanations = [], [], []

    for i in range(record_count):
        day = start_day + (i * 3) % 27
        hour = int(rng.randint(8, 20))
        minute = int(rng.randint(0, 60))
        second = int(rng.randint(0, 60))
        decision_ts = month_start.replace(day=day, hour=hour, minute=minute, second=second)
        decision_date = decision_ts.date().isoformat()

        prediction_id = str(uuid.uuid4())
        input_snapshot_id = str(uuid.uuid4())
        explanation_id = str(uuid.uuid4())
        request_trace_id = f"trace-synthetic-{batch_month.replace('-', '')}-{i + 1:04d}"

        loan_amnt = float(round(rng.uniform(5_000, 50_000), 2))
        revenue = float(round(rng.uniform(20_000, 150_000), 2))
        dti_n = float(round(rng.uniform(5.0, 35.0), 4))
        fico_n = int(rng.randint(580, 821))
        experience_c = bool(rng.randint(0, 2))
        emp_length = rng.choice(EMP_LENGTH_CATEGORIES)
        purpose = rng.choice(PURPOSE_CATEGORIES)
        home_ownership = rng.choice(HOME_OWNERSHIP_CATEGORIES)

        input_fields = {
            "loan_amnt": loan_amnt,
            "revenue": revenue,
            "dti_n": dti_n,
            "fico_n": fico_n,
            "experience_c": experience_c,
            "emp_length": emp_length,
            "purpose": purpose,
            "home_ownership_n": home_ownership,
        }
        input_hash = _canonical_input_hash(input_fields)

        features = _encode_features(input_fields)
        score = float(model.predict_score(features))
        decision = demo_decision(score, model.threshold_policy_version)

        explainer_name = EXPLAINERS_BY_INDEX[i % len(EXPLAINERS_BY_INDEX)]
        explanation = explain(features, model, explainer_name, "1.0.0")

        events.append({
            "prediction_id": prediction_id,
            "decision_ts": decision_ts,
            "decision_date": decision_date,
            "demo_decision": decision,
            "risk_score": score,
            "model_version": model_version,
            "input_snapshot_id": input_snapshot_id,
            "explanation_id": explanation_id,
            "request_trace_id": request_trace_id,
            "schema_version": config.DEFAULT_SCHEMA_VERSION,
        })
        inputs.append({
            "input_snapshot_id": input_snapshot_id,
            "prediction_id": prediction_id,
            "feature_schema_version": "loan-features-v1",
            "loan_amnt": loan_amnt,
            "revenue": revenue,
            "dti_n": dti_n,
            "fico_n": fico_n,
            "experience_c": experience_c,
            "emp_length": emp_length,
            "purpose": purpose,
            "home_ownership_n": home_ownership,
            "input_hash": input_hash,
            "created_at": decision_ts,
        })
        explanations.append({
            "explanation_id": explanation_id,
            "prediction_id": prediction_id,
            "explainer_name": explanation.explainer_name,
            "explainer_version": explanation.explainer_version,
            "explanation_json": json.dumps(explanation.explanation_json),
            "reason_codes": json.dumps(explanation.reason_codes),
            "rendered_summary": explanation.rendered_summary,
            "explanation_hash": explanation.explanation_hash,
            "created_at": decision_ts,
        })

    return {
        "batch_month": batch_month,
        "model_version": model_version,
        "events": events,
        "input_snapshots": inputs,
        "explanation_records": explanations,
        "model_registry": [
            {
                "model_version": m.model_version,
                "artifact_uri": m.artifact_uri,
                "artifact_sha256": m.artifact_sha256,
                "framework": m.framework,
                "runtime_version": m.runtime_version,
                "feature_code_commit": m.feature_code_commit,
                "preprocessing_sha256": m.preprocessing_sha256,
                "training_dataset_id": m.training_dataset_id,
                "training_dataset_version": m.training_dataset_version,
                "training_dataset_sha256": m.training_dataset_sha256,
                "training_window_start": m.training_window_start,
                "training_window_end": m.training_window_end,
                "label_definition": m.label_definition,
                "threshold_policy_version": m.threshold_policy_version,
                "explainer_compatibility": m.explainer_compatibility,
                "registered_at": m.registered_at,
            }
            for m in [models.get(model_version)]
        ],
    }


def write_parquet(batch: dict[str, Any], out_dir: Path) -> list[dict[str, Any]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written_at = datetime.now(timezone.utc)
    snapshot_id = str(uuid.uuid4())
    entries: list[dict[str, Any]] = []

    tables = [
        ("prediction_events", batch["events"], EVENTS_SCHEMA),
        ("input_snapshots", batch["input_snapshots"], INPUT_SNAPSHOTS_SCHEMA),
        ("explanation_records", batch["explanation_records"], EXPLANATION_SCHEMA),
        ("model_registry", batch["model_registry"], MODEL_REGISTRY_SCHEMA),
    ]

    for table_name, rows, schema in tables:
        if not rows:
            continue
        path = out_dir / f"{table_name}.parquet"
        converted: dict[str, list] = {}
        for field in schema:
            name = field.name
            values = [r[name] for r in rows]
            if pa.types.is_timestamp(field.type):
                values = [
                    v if isinstance(v, datetime) else _parse_ts(v)
                    if isinstance(v, str) else v
                    for v in values
                ]
            if pa.types.is_date32(field.type):
                values = [
                    v if isinstance(v, datetime) else datetime.strptime(v, "%Y-%m-%d")
                    if isinstance(v, str) else v
                    for v in values
                ]
            if pa.types.is_decimal(field.type):
                values = [Decimal(str(v)) for v in values]
            converted[name] = values
        table = pa.Table.from_pydict(converted, schema=schema)
        pq.write_table(table, path, compression="zstd")
        ts_values = [
            v for v in (converted.get("decision_ts") or converted.get("created_at") or [])
            if isinstance(v, datetime)
        ]
        entries.append({
            "batch_month": batch["batch_month"],
            "table_name": table_name,
            "path": f"{table_name}.parquet",
            "record_count": len(rows),
            "min_ts": min(ts_values).isoformat() + "Z" if ts_values else None,
            "max_ts": max(ts_values).isoformat() + "Z" if ts_values else None,
            "snapshot_id": snapshot_id,
            "written_at": written_at.isoformat().replace("+00:00", "Z"),
            "schema_version": config.DEFAULT_SCHEMA_VERSION,
        })

    manifest = {
        "batch_month": batch["batch_month"],
        "model_version": batch["model_version"],
        "record_count": len(batch["events"]),
        "snapshot_id": snapshot_id,
        "written_at": written_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
        "entries": entries,
    }
    (out_dir / "batch_manifest.json").write_text(json.dumps(manifest, indent=2))
    return entries


def main() -> None:
    """Generate two monthly production-snapshot batches (model iteration demo)."""
    batches = [
        ("2026-10", 500, 20261001, "loan-risk-demo-v1.0.0"),
        ("2026-11", 500, 20261101, "loan-risk-demo-v1.1.0"),
    ]
    for batch_month, record_count, seed, model_version in batches:
        batch = generate_batch(batch_month, record_count, seed, model_version)
        out_dir = BATCHES_DIR / batch_month
        entries = write_parquet(batch, out_dir)
        print(
            f"Wrote {len(batch['events'])} records for {batch_month} "
            f"(model {model_version}) to {out_dir}"
        )
    print("Done. Use EXPLANATION_REPO_BACKEND=duckdb to query these batches.")


if __name__ == "__main__":
    main()
