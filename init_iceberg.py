"""Initialize Apache Iceberg tables and load existing synthetic Parquet data.

Usage:
    python init_iceberg.py

This creates an Iceberg warehouse at iceberg_warehouse/ (or
$ICEBERG_WAREHOUSE), creates the four audit tables matching
iceberg_schema.sql, and loads all Parquet batches from
person2_query_interface/data/monthly_batches/*/ into them.

After running, query with:

    EXPLANATION_REPO_BACKEND=iceberg uvicorn person2_query_interface.app.service:app
"""

from __future__ import annotations

import sys
from pathlib import Path

import pyarrow.parquet as pq

# Ensure repo root on path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from person2_query_interface.app import config  # noqa: E402

BATCHES_DIR = config.MONTHLY_BATCHES_DIR

TABLE_FILES = {
    "prediction_events": "prediction_events.parquet",
    "input_snapshots": "input_snapshots.parquet",
    "explanation_records": "explanation_records.parquet",
    "model_registry": "model_registry.parquet",
}


def main() -> None:
    from pyiceberg.catalog import load_catalog

    warehouse = config.ICEBERG_WAREHOUSE
    catalog_name = config.ICEBERG_CATALOG_NAME
    namespace = config.ICEBERG_NAMESPACE

    # Create warehouse directory if needed
    Path(warehouse).mkdir(parents=True, exist_ok=True)

    print(f"Initializing Iceberg catalog '{catalog_name}' at {warehouse}")
    print(f"Namespace: {namespace}")

    catalog = load_catalog(
        catalog_name,
        **{
            "type": "sql",
            "warehouse": f"file://{warehouse}",
            "uri": f"sqlite:///{warehouse}/catalog.db",
        },
    )

    # Create namespace
    try:
        catalog.create_namespace(namespace)
        print(f"  Created namespace '{namespace}'")
    except Exception as e:
        print(f"  Namespace '{namespace}' already exists: {e}")

    # Ensure tables exist via IcebergRepository (idempotent)
    from person2_query_interface.app.repository import IcebergRepository

    repo = IcebergRepository()

    batch_dirs = sorted(
        [d for d in BATCHES_DIR.iterdir() if d.is_dir()],
    )
    if not batch_dirs:
        print(f"ERROR: No batch directories found in {BATCHES_DIR}")
        sys.exit(1)

    print(f"Found {len(batch_dirs)} batch directory(ies): {[d.name for d in batch_dirs]}")

    # Load data from each batch into each table
    for table_name, file_name in TABLE_FILES.items():
        tbl = repo._table(table_name)
        total_rows = 0
        for batch_dir in batch_dirs:
            pq_path = batch_dir / file_name
            if not pq_path.exists():
                print(f"    Skipping {batch_dir.name}/{file_name} (not found)")
                continue
            arrow_table = pq.read_table(pq_path)
            tbl.append(arrow_table)
            total_rows += arrow_table.num_rows
            print(
                f"    Loaded {arrow_table.num_rows} rows from {batch_dir.name}/{file_name}"
            )
        print(f"  Total rows in '{table_name}': {total_rows}")

    print("\nIceberg initialization complete.")
    print(f"  Warehouse: {warehouse}")
    print(f"  Catalog: {catalog_name}")
    print(f"  Namespace: {namespace}")
    print("\nNext step:")
    print(f"  EXPLANATION_REPO_BACKEND=iceberg uvicorn person2_query_interface.app.service:app")


if __name__ == "__main__":
    main()
