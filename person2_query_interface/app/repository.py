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
# JSON fixture repository
# --------------------------------------------------------------------------- #
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
    raise ValueError(f"Unknown repository backend: {backend}")
