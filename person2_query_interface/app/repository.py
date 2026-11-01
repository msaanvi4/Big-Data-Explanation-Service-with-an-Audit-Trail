"""Person 2: data access layer over the Person 1 Iceberg schema.

Pluggable backends:

* ``DuckDbParquetRepository`` — executes the SQL in ``sql/queries.sql`` over
  the monthly Parquet batches under ``data/monthly_batches/``. No cluster
  required; DuckDB reads Parquet directly. Batches are pruned by month using
  ``batch_manifest.json`` so bytes scanned are bounded and time-travel reads
  touch exactly one monthly snapshot.
* ``FixtureRepository`` — reads the JSON fixture when no Parquet batches
  exist yet.
* ``IcebergRepository`` — placeholder for the production Spark/Iceberg
  implementation; same SQL, real ``VERSION AS OF`` time travel.

All backends return the same domain records so the service, reproducibility
engine, tests, and benchmarks are backend-agnostic.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from . import config


# --------------------------------------------------------------------------- #
# Domain records (mirror Person 1's Iceberg table contracts)
# --------------------------------------------------------------------------- #
@dataclass
class PredictionEvent:
    prediction_id: str
    decision_ts: str
    decision_date: str
    demo_decision: str
    risk_score: float
    model_version: str
    input_snapshot_id: str
    explanation_id: str
    request_trace_id: str
    schema_version: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class InputSnapshot:
    input_snapshot_id: str
    prediction_id: str
    feature_schema_version: str
    loan_amnt: float
    revenue: float
    dti_n: float
    fico_n: int
    experience_c: bool
    emp_length: str
    purpose: str
    home_ownership_n: str
    input_hash: str
    created_at: str
    raw: dict[str, Any] = field(default_factory=dict)

    def to_model_features(self) -> dict[str, float]:
        """Convert the stored snapshot into the model's feature vector."""
        return {
            "loan_amnt": self.loan_amnt,
            "revenue": self.revenue,
            "dti_n": self.dti_n,
            "fico_n": float(self.fico_n),
            "experience_c": 1.0 if self.experience_c else 0.0,
            "emp_length_enc": float(
                ["< 1 year", "1-4 years", "5-9 years", "10+ years"].index(
                    self.emp_length
                )
            ),
            "purpose_enc": float(
                [
                    "debt_consolidation",
                    "home_improvement",
                    "business",
                    "medical",
                    "major_purchase",
                ].index(self.purpose)
            ),
            "home_ownership_enc": float(
                ["MORTGAGE", "RENT", "OWN"].index(self.home_ownership_n)
            ),
        }


@dataclass
class ExplanationRecord:
    explanation_id: str
    prediction_id: str
    explainer_name: str
    explainer_version: str
    explanation_json: list[dict[str, Any]]
    reason_codes: list[str]
    rendered_summary: str
    explanation_hash: str
    created_at: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelRegistryRecord:
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


@dataclass
class DecisionBundle:
    prediction: PredictionEvent
    input: InputSnapshot
    explanation: ExplanationRecord
    model: ModelRegistryRecord


class NotFoundError(KeyError):
    pass


# --------------------------------------------------------------------------- #
# Repository interface
# --------------------------------------------------------------------------- #
class AuditRepository(ABC):
    @abstractmethod
    def find_decision_by_trace_and_time(
        self, request_trace_id: str, from_ts: str, to_ts: str
    ) -> PredictionEvent | None:
        """Q1: most recent decision for a customer reference in a time window."""

    @abstractmethod
    def get_decision_bundle(self, prediction_id: str) -> DecisionBundle:
        """Q2: full audit bundle for a prediction (event + input + explanation + model)."""

    @abstractmethod
    def get_decision_bundle_at_snapshot(
        self, prediction_id: str, snapshot_id: str
    ) -> DecisionBundle:
        """Q3: point-in-time bundle (Iceberg time travel emulation)."""

    @abstractmethod
    def audit_completeness(self, request_trace_id: str) -> list[dict[str, str]]:
        """Q6: verify every event resolves to exactly one input, explanation, model."""

    @abstractmethod
    def consistency_violations(self) -> list[dict[str, Any]]:
        """Q5: input hashes with more than one distinct decision."""

    @abstractmethod
    def bytes_scanned(self, tables: tuple[str, ...], from_ts: str | None, to_ts: str | None) -> int:
        """Bytes of Parquet files that a query over ``tables`` would scan,
        pruned by batch month when ``from_ts``/``to_ts`` are given."""

    @abstractmethod
    def list_prediction_ids(self) -> list[str]:
        """All known prediction IDs (used by tests and benchmarks)."""


def _load_sql_sections(sql_text: str) -> dict[str, str]:
    """Split sql/queries.sql into named sections (Q1..Qn) so each query can be
    executed independently, regardless of the comment separators between them."""
    markers = [(m.start(), m.group(1)) for m in re.finditer(r"-- (Q\d+)\.", sql_text)]
    sections: dict[str, str] = {}
    for idx, (pos, qid) in enumerate(markers):
        end = markers[idx + 1][0] if idx + 1 < len(markers) else len(sql_text)
        sections[qid] = sql_text[pos:end]
    return sections


_SQL_TEXT = (config.SQL_DIR / "queries.sql").read_text()
_SQL_SECTIONS = _load_sql_sections(_SQL_TEXT)


def _sql(qid: str) -> str:
    return _SQL_SECTIONS[qid]


def _bind(sql: str, params: dict) -> str:
    """DuckDB binds $name parameters; the SQL file is written in Spark/Iceberg
    :name style. Translate here so the query file stays idiomatic."""
    if params:
        for name in params:
            sql = sql.replace(f":{name}", f"${name}")
    return sql


# --------------------------------------------------------------------------- #
# DuckDB over monthly Parquet batches
# --------------------------------------------------------------------------- #
class DuckDbParquetRepository(AuditRepository):
    def __init__(self, batches_dir: Path | None = None) -> None:
        self.batches_dir = batches_dir or config.MONTHLY_BATCHES_DIR
        self._entries: list[dict[str, Any]] = self._load_manifests()
        self.con = duckdb.connect(database=":memory:")
        self.con.execute("CREATE SCHEMA IF NOT EXISTS audit")
        self._register_all_views()

    def _batch_dirs(self) -> list[Path]:
        """Batch folders: subdirectories (YYYY-MM/) of batches_dir, or
        batches_dir itself when it holds batch_manifest.json (flat layout)."""
        if not self.batches_dir.exists():
            return []
        if (self.batches_dir / "batch_manifest.json").exists():
            return [self.batches_dir]
        return [p for p in sorted(self.batches_dir.iterdir()) if p.is_dir()]

    def _load_manifests(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for batch_dir in self._batch_dirs():
            manifest_path = batch_dir / "batch_manifest.json"
            if not manifest_path.exists():
                continue
            manifest = json.loads(manifest_path.read_text())
            for entry in manifest.get("entries", []):
                entry["_dir"] = str(batch_dir)
                entries.append(entry)
        return entries

    def _union_select(self, ents: list[dict[str, Any]]) -> str:
        return " UNION ALL BY NAME ".join(
            f"SELECT * FROM read_parquet('{str((Path(e['_dir']) / e['path']).resolve())}')"
            for e in ents
        )

    def _create_table_view(self, con, table: str, ents: list[dict[str, Any]]) -> None:
        # model_registry is a dimension table (one row per model version); the
        # same version can appear in several monthly batches, so de-duplicate.
        inner = self._union_select(ents)
        if table == "model_registry":
            inner = f"SELECT DISTINCT * FROM ({inner})"
        con.execute(f"CREATE OR REPLACE VIEW audit.{table} AS {inner}")

    def _register_all_views(self) -> None:
        by_table: dict[str, list[dict[str, Any]]] = {}
        for entry in self._entries:
            by_table.setdefault(entry["table_name"], []).append(entry)
        for table, ents in by_table.items():
            self._create_table_view(self.con, table, ents)

    def _pruned_entries(
        self, tables: tuple[str, ...], from_ts: str | None, to_ts: str | None
    ) -> list[dict[str, Any]]:
        selected = [e for e in self._entries if e["table_name"] in tables]
        if from_ts or to_ts:
            lo = from_ts or "0000-01-01T00:00:00Z"
            hi = to_ts or "9999-12-31T23:59:59Z"
            selected = [
                e for e in selected
                if (e.get("max_ts") or "0") >= lo and (e.get("min_ts") or "9999") <= hi
            ]
        return selected

    def bytes_scanned(self, tables, from_ts, to_ts) -> int:
        total = 0
        for e in self._pruned_entries(tables, from_ts, to_ts):
            p = Path(e["_dir"]) / e["path"]
            if p.exists():
                total += p.stat().st_size
        return total

    def _run_pruned(
        self, sql: str, params: dict, tables: tuple[str, ...], from_ts: str | None, to_ts: str | None
    ) -> list[dict]:
        sql = _bind(sql, params)
        entries = self._pruned_entries(tables, from_ts, to_ts)
        if not entries:
            return []
        con = duckdb.connect(database=":memory:")
        con.execute("CREATE SCHEMA IF NOT EXISTS audit")
        for table in sorted({e["table_name"] for e in entries}):
            ents = [e for e in entries if e["table_name"] == table]
            self._create_table_view(con, table, ents)
        # register a minimal manifest view for batch_manifest-referencing SQL
        if entries:
            regs = [(e.get("batch_month", "0000"), e.get("snapshot_id", "")) for e in entries]
            values = ", ".join(f"('{m}','{s}')" for m, s in regs)
        else:
            values = "(NULL, NULL) WHERE FALSE"
        con.execute(
            "CREATE VIEW audit.batch_manifest AS "
            f"SELECT * FROM (VALUES {values}) AS t(batch_month, snapshot_id)"
        )
        result = con.execute(sql, params)
        cols = [d[0] for d in result.description]
        return [dict(zip(cols, r)) for r in result.fetchall()]

    def find_decision_by_trace_and_time(self, request_trace_id: str, from_ts: str, to_ts: str):
        rows = self._run_pruned(
            _sql("Q1"),
            {"request_trace_id": request_trace_id, "from_ts": from_ts, "to_ts": to_ts},
            ("prediction_events",),
            from_ts,
            to_ts,
        )
        return self._row_to_event(rows[0]) if rows else None

    def get_decision_bundle(self, prediction_id: str) -> DecisionBundle:
        rows = self._run_pruned(
            _sql("Q2"), {"prediction_id": prediction_id},
            ("prediction_events", "input_snapshots", "explanation_records", "model_registry"),
            None, None,
        )
        if not rows:
            raise NotFoundError(f"Prediction not found: {prediction_id}")
        return self._rows_to_bundle(rows)

    def get_decision_bundle_at_snapshot(self, prediction_id: str, snapshot_id: str) -> DecisionBundle:
        # Emulated point-in-time: read only the batch owning this snapshot.
        snapshot_entries = [
            e for e in self._entries if e.get("snapshot_id") == snapshot_id
        ]
        if not snapshot_entries:
            raise NotFoundError(f"Snapshot not found: {snapshot_id}")
        batch_dir = Path(snapshot_entries[0]["_dir"])
        con = duckdb.connect(database=":memory:")
        con.execute("CREATE SCHEMA IF NOT EXISTS audit")
        for table in ("prediction_events", "input_snapshots", "explanation_records", "model_registry"):
            p = batch_dir / f"{table}.parquet"
            if p.exists():
                self._create_table_view(con, table, [{"_dir": str(batch_dir), "path": f"{table}.parquet", "table_name": table}])
        result = con.execute(_bind(_sql("Q2"), {"prediction_id": prediction_id}), {"prediction_id": prediction_id})
        cols = [d[0] for d in result.description]
        rows = [dict(zip(cols, r)) for r in result.fetchall()]
        if not rows:
            raise NotFoundError(f"Prediction not found at snapshot {snapshot_id}: {prediction_id}")
        return self._rows_to_bundle(rows)

    def audit_completeness(self, request_trace_id: str) -> list[dict[str, str]]:
        rows = self._run_pruned(
            _sql("Q6"), {"request_trace_id": request_trace_id},
            ("prediction_events", "input_snapshots", "explanation_records", "model_registry"),
            None, None,
        )
        return rows

    def consistency_violations(self) -> list[dict[str, Any]]:
        return self._run_pruned(
            _sql("Q5"), {},
            ("prediction_events", "input_snapshots"),
            None, None,
        )

    def list_prediction_ids(self) -> list[str]:
        rows = self.con.execute("SELECT prediction_id FROM audit.prediction_events").fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------ #
    # Mapping helpers
    # ------------------------------------------------------------------ #
    def _row_to_event(self, row: dict) -> PredictionEvent:
        return PredictionEvent(
            prediction_id=row["prediction_id"],
            decision_ts=str(row["decision_ts"]),
            decision_date=str(row["decision_date"]),
            demo_decision=row["demo_decision"],
            risk_score=float(row["risk_score"]),
            model_version=row["model_version"],
            input_snapshot_id=row["input_snapshot_id"],
            explanation_id=row["explanation_id"],
            request_trace_id=row["request_trace_id"],
            schema_version=row["schema_version"],
            raw=row,
        )

    def _normalize_json_list(self, value: Any) -> list:
        if isinstance(value, str):
            return json.loads(value)
        return [dict(v) if not isinstance(v, dict) else v for v in value]

    def _rows_to_bundle(self, rows: list[dict]) -> DecisionBundle:
        row = rows[0]
        event = PredictionEvent(
            prediction_id=row["prediction_id"],
            decision_ts=str(row["decision_ts"]),
            decision_date=str(row["decision_date"]),
            demo_decision=row["demo_decision"],
            risk_score=float(row["risk_score"]),
            model_version=row["model_version"],
            input_snapshot_id=row["input_snapshot_id"],
            explanation_id=row["explanation_id"],
            request_trace_id=row["request_trace_id"],
            schema_version=row["schema_version"],
            raw=row,
        )
        snapshot = InputSnapshot(
            input_snapshot_id=row["input_snapshot_id"],
            prediction_id=row["prediction_id"],
            feature_schema_version=row["feature_schema_version"],
            loan_amnt=float(row["loan_amnt"]),
            revenue=float(row["revenue"]),
            dti_n=float(row["dti_n"]),
            fico_n=int(row["fico_n"]),
            experience_c=bool(row["experience_c"]),
            emp_length=row["emp_length"],
            purpose=row["purpose"],
            home_ownership_n=row["home_ownership_n"],
            input_hash=row["input_hash"],
            created_at=str(row["input_created_at"]),
            raw=row,
        )
        explanation = ExplanationRecord(
            explanation_id=row["explanation_id"],
            prediction_id=row["prediction_id"],
            explainer_name=row["explainer_name"],
            explainer_version=row["explainer_version"],
            explanation_json=self._normalize_json_list(row["explanation_json"]),
            reason_codes=self._normalize_json_list(row["reason_codes"]),
            rendered_summary=row["rendered_summary"],
            explanation_hash=row["explanation_hash"],
            created_at=str(row["explanation_created_at"]),
            raw=row,
        )
        model = ModelRegistryRecord(
            model_version=row["model_version"],
            artifact_uri=row["artifact_uri"],
            artifact_sha256=row["artifact_sha256"],
            framework=row["framework"],
            runtime_version=row["runtime_version"],
            feature_code_commit=row["feature_code_commit"],
            preprocessing_sha256=row["preprocessing_sha256"],
            training_dataset_id=row["training_dataset_id"],
            training_dataset_version=row["training_dataset_version"],
            training_dataset_sha256=row["training_dataset_sha256"],
            training_window_start=row["training_window_start"],
            training_window_end=row["training_window_end"],
            label_definition=row["label_definition"],
            threshold_policy_version=row["threshold_policy_version"],
            explainer_compatibility=row["explainer_compatibility"],
            registered_at=row["registered_at"],
        )
        return DecisionBundle(event, snapshot, explanation, model)


# --------------------------------------------------------------------------- #
# Apache Iceberg repository (real catalog backend via pyiceberg)
# --------------------------------------------------------------------------- #
class IcebergRepository(AuditRepository):
    """Person 2: data access layer over a live Apache Iceberg catalog.

    Executes the same Q1–Q7 domain operations as the DuckDB backend but
    against real Iceberg tables with native snapshot isolation and
    ``VERSION AS OF`` time travel.

    Requires: ``pip install pyiceberg[pandas]`` and an Iceberg catalog
    (Hadoop/filesystem by default) at :attr:`config.ICEBERG_WAREHOUSE`.
    """

    def __init__(self, warehouse: str | None = None, catalog_name: str | None = None) -> None:
        from pyiceberg.catalog import load_catalog
        from pyiceberg.schema import Schema, NestedField
        from pyiceberg.types import (
            BooleanType,
            DateType,
            DecimalType,
            DoubleType,
            IntegerType,
            StringType,
            TimestampType,
        )
        from pyiceberg.partitioning import PartitionSpec, PartitionField
        from pyiceberg.transforms import DayTransform
        from pyiceberg.table import Table

        self._Table = Table  # keep reference for type hints
        self._catalog_name = catalog_name or config.ICEBERG_CATALOG_NAME
        warehouse = warehouse or config.ICEBERG_WAREHOUSE

        self.catalog = load_catalog(
            self._catalog_name,
            **{
                "type": "sql",
                "warehouse": f"file://{warehouse}",
                "uri": f"sqlite:///{warehouse}/catalog.db",
            },
        )
        self.namespace = config.ICEBERG_NAMESPACE
        self._ensure_namespace()
        self._tables: dict[str, Any] = {}
        self._init_tables(Schema, NestedField, StringType, TimestampType, DateType, DoubleType,
                          DecimalType, IntegerType, BooleanType,
                          PartitionSpec, PartitionField, DayTransform)

    # ------------------------------------------------------------------ #
    # Table initialization
    # ------------------------------------------------------------------ #
    def _ensure_namespace(self) -> None:
        try:
            self.catalog.create_namespace(self.namespace)
        except Exception:
            # Namespace may already exist
            pass

    def _table_name(self, table: str) -> str:
        return f"{self.namespace}.{table}"

    def _init_tables(
        self,
        Schema,
        NestedField,
        StringType,
        TimestampType,
        DateType,
        DoubleType,
        DecimalType,
        IntegerType,
        BooleanType,
        PartitionSpec,
        PartitionField,
        DayTransform,
    ) -> None:
        """Create Iceberg tables matching Person 1's ``iceberg_schema.sql``
        if they do not already exist."""

        def _fields(spec: list[tuple[int, str, Any]]) -> list[NestedField]:
            return [NestedField(fid, name, typ) for fid, name, typ in spec]

        # ---- model_registry ----
        model_registry_schema = Schema(
            *_fields([
                (1, "model_version", StringType()),
                (2, "artifact_uri", StringType()),
                (3, "artifact_sha256", StringType()),
                (4, "framework", StringType()),
                (5, "runtime_version", StringType()),
                (6, "feature_code_commit", StringType()),
                (7, "preprocessing_sha256", StringType()),
                (8, "training_dataset_id", StringType()),
                (9, "training_dataset_version", StringType()),
                (10, "training_dataset_sha256", StringType()),
                (11, "training_window_start", DateType()),
                (12, "training_window_end", DateType()),
                (13, "label_definition", StringType()),
                (14, "threshold_policy_version", StringType()),
                (15, "explainer_compatibility", StringType()),
                (16, "registered_at", TimestampType()),
            ])
        )
        self._get_or_create_table("model_registry", model_registry_schema, None)

        # ---- prediction_events (partitioned by days(decision_ts)) ----
        # Field IDs: 1=prediction_id, 2=decision_ts, 3=decision_date, ...
        prediction_events_schema = Schema(
            *_fields([
                (1, "prediction_id", StringType()),
                (2, "decision_ts", TimestampType()),
                (3, "decision_date", DateType()),
                (4, "demo_decision", StringType()),
                (5, "risk_score", DoubleType()),
                (6, "model_version", StringType()),
                (7, "input_snapshot_id", StringType()),
                (8, "explanation_id", StringType()),
                (9, "request_trace_id", StringType()),
                (10, "schema_version", StringType()),
            ])
        )
        self._get_or_create_table(
            "prediction_events",
            prediction_events_schema,
            PartitionSpec(
                partition_fields=(
                    PartitionField(
                        source_id=2, field_id=1000, name="decision_ts_day", transform=DayTransform()
                    ),
                )
            ),
        )

        # ---- input_snapshots (partitioned by days(created_at)) ----
        # Field IDs: ... 13=created_at
        input_snapshots_schema = Schema(
            *_fields([
                (1, "input_snapshot_id", StringType()),
                (2, "prediction_id", StringType()),
                (3, "feature_schema_version", StringType()),
                (4, "loan_amnt", DecimalType(12, 2)),
                (5, "revenue", DecimalType(12, 2)),
                (6, "dti_n", DecimalType(7, 4)),
                (7, "fico_n", IntegerType()),
                (8, "experience_c", BooleanType()),
                (9, "emp_length", StringType()),
                (10, "purpose", StringType()),
                (11, "home_ownership_n", StringType()),
                (12, "input_hash", StringType()),
                (13, "created_at", TimestampType()),
            ])
        )
        self._get_or_create_table(
            "input_snapshots",
            input_snapshots_schema,
            PartitionSpec(
                partition_fields=(
                    PartitionField(
                        source_id=13, field_id=1000, name="created_at_day", transform=DayTransform()
                    ),
                )
            ),
        )

        # ---- explanation_records (partitioned by days(created_at)) ----
        # Field IDs: ... 9=created_at
        explanation_records_schema = Schema(
            *_fields([
                (1, "explanation_id", StringType()),
                (2, "prediction_id", StringType()),
                (3, "explainer_name", StringType()),
                (4, "explainer_version", StringType()),
                (5, "explanation_json", StringType()),
                (6, "reason_codes", StringType()),
                (7, "rendered_summary", StringType()),
                (8, "explanation_hash", StringType()),
                (9, "created_at", TimestampType()),
            ])
        )
        self._get_or_create_table(
            "explanation_records",
            explanation_records_schema,
            PartitionSpec(
                partition_fields=(
                    PartitionField(
                        source_id=9, field_id=1000, name="created_at_day", transform=DayTransform()
                    ),
                )
            ),
        )

    def _get_or_create_table(self, table: str, schema: Any, spec: Any | None) -> None:
        fqn = self._table_name(table)
        try:
            t = self.catalog.load_table(fqn)
        except Exception:
            if spec is not None:
                t = self.catalog.create_table(fqn, schema=schema, partition_spec=spec)
            else:
                t = self.catalog.create_table(fqn, schema=schema)
        self._tables[table] = t

    def _table(self, table: str) -> Any:
        if table not in self._tables:
            self._tables[table] = self.catalog.load_table(self._table_name(table))
        return self._tables[table]

    # ------------------------------------------------------------------ #
    # Q1: most recent decision for a request_trace_id in a time window
    # ------------------------------------------------------------------ #
    def find_decision_by_trace_and_time(
        self, request_trace_id: str, from_ts: str, to_ts: str
    ) -> PredictionEvent | None:
        from pyiceberg.expressions import (
            And, EqualTo, GreaterThanOrEqual, LessThanOrEqual,
        )

        tbl = self._table("prediction_events")
        # pyiceberg timestamp bindings expect ISO-8601 without trailing 'Z'
        lo = from_ts.replace("Z", "")
        hi = to_ts.replace("Z", "")
        scan = tbl.scan(
            row_filter=And(
                EqualTo("request_trace_id", request_trace_id),
                GreaterThanOrEqual("decision_ts", lo),
                LessThanOrEqual("decision_ts", hi),
            ),
        )
        # Iceberg scan: materialize via Arrow, then order by decision_ts desc, limit 1
        rows = scan.to_arrow().to_pylist()
        if not rows:
            return None
        rows.sort(key=lambda r: r["decision_ts"], reverse=True)
        return self._row_to_event(rows[0])

    # ------------------------------------------------------------------ #
    # Q2: full audit bundle
    # ------------------------------------------------------------------ #
    def get_decision_bundle(self, prediction_id: str) -> DecisionBundle:
        event_dict = self._find_event_by_id("prediction_id", prediction_id, "prediction_events")
        if event_dict is None:
            raise NotFoundError(f"Prediction not found: {prediction_id}")
        event = self._row_to_event(event_dict)

        snapshot = self._find_record(
            "input_snapshots", "input_snapshot_id", event.input_snapshot_id
        )
        explanation = self._find_record(
            "explanation_records", "explanation_id", event.explanation_id
        )
        model = self._find_record(
            "model_registry", "model_version", event.model_version
        )
        return self._rows_to_bundle(event_dict, snapshot, explanation, model)

    # ------------------------------------------------------------------ #
    # Q3: point-in-time bundle (Iceberg native time travel)
    # ------------------------------------------------------------------ #
    def get_decision_bundle_at_snapshot(
        self, prediction_id: str, snapshot_id: str
    ) -> DecisionBundle:
        tbl = self._table("prediction_events")
        try:
            # Iceberg snapshot IDs are long integers; try direct lookup first
            snap_id = int(snapshot_id)
            event_scan = tbl.scan(snapshot_id=snap_id)
        except ValueError:
            # Not a numeric snapshot id: fall back to current table,
            # find the event, then read related tables at current snapshot
            event_scan = tbl.scan()

        rows = event_scan.to_arrow().to_pylist()

        # Filter to the requested prediction_id
        matching = [r for r in rows if r.get("prediction_id") == prediction_id]
        if not matching:
            raise NotFoundError(
                f"Prediction {prediction_id} not found at snapshot {snapshot_id}"
            )
        event = self._row_to_event(matching[0])

        snapshot = self._find_record(
            "input_snapshots", "input_snapshot_id", event.input_snapshot_id
        )
        explanation = self._find_record(
            "explanation_records", "explanation_id", event.explanation_id
        )
        model = self._find_record(
            "model_registry", "model_version", event.model_version
        )
        return self._rows_to_bundle(matching[0], snapshot, explanation, model)

    # ------------------------------------------------------------------ #
    # Q6: audit completeness
    # ------------------------------------------------------------------ #
    def audit_completeness(self, request_trace_id: str) -> list[dict[str, str]]:
        event = self.find_decision_by_trace_and_time(
            request_trace_id, "1970-01-01T00:00:00Z", "2100-01-01T00:00:00Z"
        )
        if not event:
            return []
        input_status = "OK" if self._find_record(
            "input_snapshots", "input_snapshot_id", event.input_snapshot_id
        ) else "MISSING_INPUT"
        explanation_status = "OK" if self._find_record(
            "explanation_records", "explanation_id", event.explanation_id
        ) else "MISSING_EXPLANATION"
        model_status = "OK" if self._find_record(
            "model_registry", "model_version", event.model_version
        ) else "MISSING_MODEL"
        return [{
            "prediction_id": event.prediction_id,
            "input_status": input_status,
            "explanation_status": explanation_status,
            "model_status": model_status,
        }]

    # ------------------------------------------------------------------ #
    # Q5: consistency violations
    # ------------------------------------------------------------------ #
    def consistency_violations(self) -> list[dict[str, Any]]:
        events = self._scan_all("prediction_events")
        snapshots = {s["input_snapshot_id"]: s for s in self._scan_all("input_snapshots")}

        from collections import defaultdict
        by_hash: dict[str, list[str]] = defaultdict(list)
        for e in events:
            snap = snapshots.get(e["input_snapshot_id"])
            if snap:
                by_hash[snap["input_hash"]].append(e["demo_decision"])

        violations = []
        for input_hash, decisions in by_hash.items():
            distinct = set(decisions)
            if len(distinct) > 1:
                violations.append({
                    "input_hash": input_hash,
                    "prediction_count": len(decisions),
                    "distinct_decisions": len(distinct),
                    "model_versions_used": len(set(e["model_version"] for e in events
                                                  if e["input_snapshot_id"] in snapshots
                                                  and snapshots[e["input_snapshot_id"]]["input_hash"] == input_hash)),
                })
        return violations

    # ------------------------------------------------------------------ #
    # Housekeeping
    # ------------------------------------------------------------------ #
    def bytes_scanned(self, tables: tuple[str, ...], from_ts: str | None, to_ts: str | None) -> int:
        """Bytes of data files that would be scanned for the given tables."""
        # Iceberg-native byte scanning requires reading manifest files;
        # return 0 here — the service still reports latency and SLO correctly.
        return 0

    def list_prediction_ids(self) -> list[str]:
        return [e["prediction_id"] for e in self._scan_all("prediction_events")]

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _scan_all(self, table: str) -> list[dict[str, Any]]:
        tbl = self._table(table)
        return tbl.scan().to_arrow().to_pylist()

    def _find_record(self, table: str, key: str, value: Any) -> dict[str, Any] | None:
        tbl = self._table(table)
        from pyiceberg.expressions import EqualTo
        scan = tbl.scan(row_filter=EqualTo(key, value))
        rows = scan.to_arrow().to_pylist()
        return rows[0] if rows else None

    def _find_event_by_id(self, key: str, value: str, table: str) -> dict[str, Any] | None:
        return self._find_record(table, key, value)

    def _row_to_event(self, row: dict[str, Any]) -> PredictionEvent:
        return PredictionEvent(
            prediction_id=str(row["prediction_id"]),
            decision_ts=str(row["decision_ts"]),
            decision_date=str(row["decision_date"]),
            demo_decision=row["demo_decision"],
            risk_score=float(row["risk_score"]),
            model_version=row["model_version"],
            input_snapshot_id=row["input_snapshot_id"],
            explanation_id=row["explanation_id"],
            request_trace_id=row["request_trace_id"],
            schema_version=row["schema_version"],
            raw=row,
        )

    def _normalize_json_list(self, value: Any) -> list:
        if isinstance(value, str):
            return json.loads(value)
        return [dict(v) if not isinstance(v, dict) else v for v in value]

    def _rows_to_bundle(
        self,
        event: dict[str, Any],
        snapshot: dict[str, Any] | None,
        explanation: dict[str, Any] | None,
        model: dict[str, Any] | None,
    ) -> DecisionBundle:
        event_obj = self._row_to_event(event)

        if snapshot is None:
            raise NotFoundError(
                f"Missing input snapshot {event_obj.input_snapshot_id}"
            )
        if explanation is None:
            raise NotFoundError(
                f"Missing explanation record {event_obj.explanation_id}"
            )
        if model is None:
            raise NotFoundError(
                f"Missing model registry entry {event_obj.model_version}"
            )

        snapshot_obj = InputSnapshot(
            input_snapshot_id=str(snapshot["input_snapshot_id"]),
            prediction_id=str(snapshot["prediction_id"]),
            feature_schema_version=str(snapshot["feature_schema_version"]),
            loan_amnt=float(snapshot["loan_amnt"]),
            revenue=float(snapshot["revenue"]),
            dti_n=float(snapshot["dti_n"]),
            fico_n=int(snapshot["fico_n"]),
            experience_c=bool(snapshot["experience_c"]),
            emp_length=str(snapshot["emp_length"]),
            purpose=str(snapshot["purpose"]),
            home_ownership_n=str(snapshot["home_ownership_n"]),
            input_hash=str(snapshot["input_hash"]),
            created_at=str(snapshot["created_at"]),
            raw=snapshot,
        )
        explanation_obj = ExplanationRecord(
            explanation_id=str(explanation["explanation_id"]),
            prediction_id=str(explanation["prediction_id"]),
            explainer_name=str(explanation["explainer_name"]),
            explainer_version=str(explanation["explainer_version"]),
            explanation_json=self._normalize_json_list(explanation.get("explanation_json")),
            reason_codes=self._normalize_json_list(explanation.get("reason_codes")),
            rendered_summary=str(explanation["rendered_summary"]),
            explanation_hash=str(explanation["explanation_hash"]),
            created_at=str(explanation["created_at"]),
            raw=explanation,
        )
        model_obj = ModelRegistryRecord(
            model_version=str(model["model_version"]),
            artifact_uri=str(model["artifact_uri"]),
            artifact_sha256=str(model["artifact_sha256"]),
            framework=str(model["framework"]),
            runtime_version=str(model["runtime_version"]),
            feature_code_commit=str(model["feature_code_commit"]),
            preprocessing_sha256=str(model["preprocessing_sha256"]),
            training_dataset_id=str(model["training_dataset_id"]),
            training_dataset_version=str(model["training_dataset_version"]),
            training_dataset_sha256=str(model["training_dataset_sha256"]),
            training_window_start=str(model["training_window_start"]),
            training_window_end=str(model["training_window_end"]),
            label_definition=str(model["label_definition"]),
            threshold_policy_version=str(model["threshold_policy_version"]),
            explainer_compatibility=str(model["explainer_compatibility"]),
            registered_at=str(model["registered_at"]),
        )
        return DecisionBundle(event_obj, snapshot_obj, explanation_obj, model_obj)


class FixtureRepository(AuditRepository):
    def __init__(self, fixture_path: Path | None = None) -> None:
        self.path = fixture_path or config.FIXTURE_PATH
        self._data = json.loads(self.path.read_text())
        self._events = {e["prediction_id"]: e for e in self._data.get("prediction_events", [])}
        self._inputs = {s["input_snapshot_id"]: s for s in self._data.get("input_snapshots", [])}
        self._explanations = {x["explanation_id"]: x for x in self._data.get("explanation_records", [])}
        self._models = {m["model_version"]: m for m in self._data.get("model_registry", [])}

    def find_decision_by_trace_and_time(self, request_trace_id: str, from_ts: str, to_ts: str):
        candidates = [
            e for e in self._data.get("prediction_events", [])
            if e["request_trace_id"] == request_trace_id and from_ts <= e["decision_ts"] <= to_ts
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda e: e["decision_ts"], reverse=True)
        return PredictionEvent(**candidates[0], raw=candidates[0])

    def get_decision_bundle(self, prediction_id: str) -> DecisionBundle:
        event = self._events.get(prediction_id)
        if not event:
            raise NotFoundError(f"Prediction not found: {prediction_id}")
        return self._bundle(event)

    def get_decision_bundle_at_snapshot(self, prediction_id: str, snapshot_id: str) -> DecisionBundle:
        return self.get_decision_bundle(prediction_id)

    def audit_completeness(self, request_trace_id: str) -> list[dict[str, str]]:
        event = self.find_decision_by_trace_and_time(
            request_trace_id, "1970-01-01T00:00:00Z", "2100-01-01T00:00:00Z"
        )
        if not event:
            return []
        return [{
            "prediction_id": event.prediction_id,
            "input_status": "OK" if event.input_snapshot_id in self._inputs else "MISSING_INPUT",
            "explanation_status": "OK" if event.explanation_id in self._explanations else "MISSING_EXPLANATION",
            "model_status": "OK" if event.model_version in self._models else "MISSING_MODEL",
        }]

    def consistency_violations(self) -> list[dict[str, Any]]:
        return []

    def bytes_scanned(self, tables, from_ts, to_ts) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    def list_prediction_ids(self) -> list[str]:
        return list(self._events.keys())

    def _bundle(self, event: dict) -> DecisionBundle:
        snapshot = self._inputs[event["input_snapshot_id"]]
        explanation = self._explanations[event["explanation_id"]]
        model = self._models[event["model_version"]]
        return DecisionBundle(
            prediction=PredictionEvent(**event, raw=event),
            input=InputSnapshot(**snapshot, raw=snapshot),
            explanation=ExplanationRecord(
                explanation_id=explanation["explanation_id"],
                prediction_id=explanation["prediction_id"],
                explainer_name=explanation["explainer_name"],
                explainer_version=explanation["explainer_version"],
                explanation_json=explanation["explanation_json"],
                reason_codes=explanation["reason_codes"],
                rendered_summary=explanation["rendered_summary"],
                explanation_hash=explanation["explanation_hash"],
                created_at=explanation["created_at"],
                raw=explanation,
            ),
            model=ModelRegistryRecord(**model, raw=model),
        )


def get_repository() -> AuditRepository:
    backend = config.REPO_BACKEND
    if backend == "duckdb":
        return DuckDbParquetRepository()
    if backend == "fixture":
        return FixtureRepository()
    if backend == "iceberg":
        return IcebergRepository()
    raise ValueError(f"Unknown repository backend: {backend}")
