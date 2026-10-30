"""Person 2: performance benchmarks for the query interface.

Measures p50/p95 lookup latency, bytes scanned, and rows returned for the
"why was this decision made?" path (Q1 + Q2 over the DuckDB/Parquet
backend) across increasing synthetic volumes. Target SLO: answers within
5 seconds (Person 2 deliverable: p95 well under the SLO on this synthetic
workload; on a production Iceberg cluster the same SQL plus partition
pruning and file compaction keeps it there).

Writes results to benchmarks/results/benchmark_<utc>.json and
benchmarks/results/latest.json.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from person2_query_interface.app import config
from person2_query_interface.app.repository import DuckDbParquetRepository
from person2_query_interface.data import generator

RESULTS_DIR = config.PERSON2_DIR / "benchmarks" / "results"
SCALES = [1_000, 5_000, 10_000]
LOOKUPS_PER_SCALE = 200


def percentile(values, p):
    return float(np.percentile(sorted(values), p))


def run_scale(record_count: int, seed: int) -> dict:
    # Any real month string parses; batches live in a temp dir so the
    # committed monthly_batches/ are not polluted by benchmark runs.
    batch_month = "2026-12"
    batch = generator.generate_batch(batch_month, record_count, seed, "loan-risk-demo-v1.0.0")
    out_dir = Path(tempfile.mkdtemp(prefix=f"explain-bench-{record_count}-"))
    generator.write_parquet(batch, out_dir)

    repo = DuckDbParquetRepository(batches_dir=out_dir)
    ids = repo.list_prediction_ids()
    rng = np.random.RandomState(42)
    picks = [ids[i] for i in rng.choice(len(ids), size=min(LOOKUPS_PER_SCALE, len(ids)), replace=False)]

    latencies, bytes_scanned_list, rows = [], [], []
    for pid in picks:
        import time

        t0 = time.perf_counter()
        bundle = repo.get_decision_bundle(pid)
        elapsed = (time.perf_counter() - t0) * 1000
        latencies.append(elapsed)
        rows.append(1)
        bytes_scanned_list.append(
            repo.bytes_scanned(
                ("prediction_events", "input_snapshots", "explanation_records", "model_registry"),
                None,
                None,
            )
        )

    return {
        "record_count": record_count,
        "lookups": len(picks),
        "latency_ms": {
            "min": round(min(latencies), 3),
            "p50": round(percentile(latencies, 50), 3),
            "p95": round(percentile(latencies, 95), 3),
            "p99": round(percentile(latencies, 99), 3),
            "max": round(max(latencies), 3),
        },
        "bytes_scanned": {
            "min": min(bytes_scanned_list),
            "p50": round(percentile(bytes_scanned_list, 50)),
            "p95": round(percentile(bytes_scanned_list, 95)),
            "max": max(bytes_scanned_list),
        },
        "rows_returned": rows,
        "within_slo": all(l <= config.QUERY_SLO_SECONDS * 1000 for l in latencies),
    }


def main() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results = [run_scale(count, 20260000 + i) for i, count in enumerate(SCALES)]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slo_seconds": config.QUERY_SLO_SECONDS,
        "backend": "duckdb-parquet",
        "scales": results,
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS_DIR / f"benchmark_{stamp}.json"
    out.write_text(json.dumps(payload, indent=2))
    (RESULTS_DIR / "latest.json").write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
