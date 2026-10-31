
"""
Person 3: Prediction Chain of Custody

Maintains a verifiable history of events associated with
each prediction.

Each event records:
- Prediction ID
- Event type
- Actor and role
- Timestamp
- Artifact type and SHA-256 hash
- Event metadata
- Previous event hash
- Current event hash

Events are linked separately for each prediction.
This is a SQLite prototype using synthetic project data.
"""

import hashlib
import json
import sqlite3
import uuid

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_DB_PATH = (
    Path(__file__).resolve().parent / "custody.db"
)

GENESIS_HASH = "0" * 64

VALID_EVENT_TYPES = {
    "PREDICTION_CREATED",
    "INPUT_SNAPSHOT_CAPTURED",
    "MODEL_APPLIED",
    "EXPLANATION_GENERATED",
    "PREDICTION_STORED",
    "PREDICTION_ACCESSED",
    "PREDICTION_REPLAYED",
    "PREDICTION_EXPORTED"
}


class ChainOfCustody:

    def __init__(self, db_path=None):

        self.db_path = (
            Path(db_path) if db_path else DEFAULT_DB_PATH
        )

        self.db_path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        self.create_table()

    # --------------------------------------------------
    # DATABASE CONNECTION
    # --------------------------------------------------

    @contextmanager
    def get_connection(self):

        conn = sqlite3.connect(
            self.db_path,
            timeout=30
        )

        conn.row_factory = sqlite3.Row

        try:
            yield conn
            conn.commit()

        except Exception:
            conn.rollback()
            raise

        finally:
            conn.close()

    # --------------------------------------------------
    # CREATE CUSTODY TABLE
    # --------------------------------------------------

    def create_table(self):

        with self.get_connection() as conn:

            conn.execute("""
                CREATE TABLE IF NOT EXISTS custody_events (

                    seq INTEGER PRIMARY KEY AUTOINCREMENT,

                    event_id TEXT UNIQUE NOT NULL,

                    prediction_id TEXT NOT NULL,

                    event_type TEXT NOT NULL,

                    actor_id TEXT NOT NULL,

                    actor_role TEXT NOT NULL,

                    event_timestamp TEXT NOT NULL,

                    artifact_type TEXT,

                    artifact_hash TEXT,

                    metadata_json TEXT NOT NULL,

                    previous_hash TEXT NOT NULL,

                    record_hash TEXT NOT NULL

                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_custody_prediction
                ON custody_events(prediction_id, seq)
            """)

    # --------------------------------------------------
    # HASH GENERATION
    # --------------------------------------------------

    @staticmethod
    def calculate_hash(record):
        """
        Generate SHA-256 hash from canonical JSON.
        """

        canonical_json = json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":")
        )

        return hashlib.sha256(
            canonical_json.encode("utf-8")
        ).hexdigest()

    # --------------------------------------------------
    # CALCULATE ARTIFACT HASH
    # --------------------------------------------------

    @staticmethod
    def calculate_artifact_hash(artifact):
        """
        Calculate SHA-256 hash for an artifact.

        Pass a string, bytes, or a JSON-compatible object.
        """

        if isinstance(artifact, bytes):
            content = artifact

        elif isinstance(artifact, str):
            content = artifact.encode("utf-8")

        else:
            content = json.dumps(
                artifact,
                sort_keys=True,
                separators=(",", ":")
            ).encode("utf-8")

        return hashlib.sha256(content).hexdigest()

    # --------------------------------------------------
    # RECORD CUSTODY EVENT
    # --------------------------------------------------

    def record_event(
        self,
        prediction_id,
        event_type,
        actor_id,
        actor_role,
        artifact_type=None,
        artifact_hash=None,
        metadata=None
    ):
        """
        Record one lifecycle event for a prediction.

        Each prediction has its own hash chain.
        """

        if not prediction_id or not str(prediction_id).strip():
            raise ValueError("prediction_id is required")

        if not actor_id or not str(actor_id).strip():
            raise ValueError("actor_id is required")

        if not actor_role or not str(actor_role).strip():
            raise ValueError("actor_role is required")

        event_type = str(event_type).upper()

        if event_type not in VALID_EVENT_TYPES:
            raise ValueError(
                f"Invalid event type: {event_type}"
            )

        if artifact_hash is not None:
            if (
                len(str(artifact_hash)) != 64
                or any(
                    c not in "0123456789abcdef"
                    for c in str(artifact_hash).lower()
                )
            ):
                raise ValueError(
                    "artifact_hash must be a 64-character SHA-256 hex digest"
                )

        if metadata is None:
            metadata = {}

        if not isinstance(metadata, dict):
            raise ValueError("metadata must be a dictionary")

        event_id = str(uuid.uuid4())

        event_timestamp = datetime.now(
            timezone.utc
        ).isoformat()

        metadata_json = json.dumps(
            metadata,
            sort_keys=True
        )

        with self.get_connection() as conn:

            conn.execute("BEGIN IMMEDIATE")

            previous = conn.execute("""
                SELECT record_hash
                FROM custody_events
                WHERE prediction_id = ?
                ORDER BY seq DESC
                LIMIT 1
            """, (prediction_id,)).fetchone()

            previous_hash = (
                previous["record_hash"]
                if previous
                else GENESIS_HASH
            )

            record = {
                "event_id": event_id,
                "prediction_id": str(prediction_id),
                "event_type": event_type,
                "actor_id": str(actor_id),
                "actor_role": str(actor_role).upper(),
                "event_timestamp": event_timestamp,
                "artifact_type": artifact_type,
                "artifact_hash": artifact_hash,
                "metadata_json": metadata_json,
                "previous_hash": previous_hash
            }

            record_hash = self.calculate_hash(record)

            conn.execute("""
                INSERT INTO custody_events (
                    event_id,
                    prediction_id,
                    event_type,
                    actor_id,
                    actor_role,
                    event_timestamp,
                    artifact_type,
                    artifact_hash,
                    metadata_json,
                    previous_hash,
                    record_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event_id,
                str(prediction_id),
                event_type,
                str(actor_id),
                str(actor_role).upper(),
                event_timestamp,
                artifact_type,
                artifact_hash,
                metadata_json,
                previous_hash,
                record_hash
            ))

            record["record_hash"] = record_hash

            return record

    # --------------------------------------------------
    # RECORD PREDICTION LIFECYCLE
    # --------------------------------------------------

    def record_prediction_lifecycle(
        self,
        prediction_id,
        actor_id,
        actor_role,
        input_snapshot,
        model_version,
        explanation
    ):
        """
        Convenience method to record the five main
        prediction lifecycle stages.

        Artifacts are hashed before being recorded.
        """

        input_hash = self.calculate_artifact_hash(
            input_snapshot
        )

        explanation_hash = self.calculate_artifact_hash(
            explanation
        )

        events = []

        events.append(self.record_event(
            prediction_id=prediction_id,
            event_type="PREDICTION_CREATED",
            actor_id=actor_id,
            actor_role=actor_role,
            metadata={"source": "prediction_service"}
        ))

        events.append(self.record_event(
            prediction_id=prediction_id,
            event_type="INPUT_SNAPSHOT_CAPTURED",
            actor_id=actor_id,
            actor_role=actor_role,
            artifact_type="INPUT_SNAPSHOT",
            artifact_hash=input_hash
        ))

        events.append(self.record_event(
            prediction_id=prediction_id,
            event_type="MODEL_APPLIED",
            actor_id=actor_id,
            actor_role=actor_role,
            artifact_type="MODEL_VERSION",
            metadata={"model_version": model_version}
        ))

        events.append(self.record_event(
            prediction_id=prediction_id,
            event_type="EXPLANATION_GENERATED",
            actor_id=actor_id,
            actor_role=actor_role,
            artifact_type="EXPLANATION",
            artifact_hash=explanation_hash
        ))

        events.append(self.record_event(
            prediction_id=prediction_id,
            event_type="PREDICTION_STORED",
            actor_id=actor_id,
            actor_role=actor_role,
            metadata={"storage_status": "STORED"}
        ))

        return events

    # --------------------------------------------------
    # GET EVENTS FOR A PREDICTION
    # --------------------------------------------------

    def get_prediction_history(self, prediction_id):

        with self.get_connection() as conn:

            rows = conn.execute("""
                SELECT *
                FROM custody_events
                WHERE prediction_id = ?
                ORDER BY seq ASC
            """, (prediction_id,)).fetchall()

            events = []

            for row in rows:

                event = dict(row)

                event["metadata"] = json.loads(
                    event.pop("metadata_json")
                )

                events.append(event)

            return events

    # --------------------------------------------------
    # VERIFY CUSTODY CHAIN
    # --------------------------------------------------

    def verify_prediction_chain(self, prediction_id):
        """
        Verify the hash chain for one prediction.
        """

        events = self.get_prediction_history(
            prediction_id
        )

        if not events:
            return {
                "valid": False,
                "prediction_id": prediction_id,
                "checked_events": 0,
                "error": "No custody events found"
            }

        expected_previous_hash = GENESIS_HASH

        for event in events:

            record = {
                "event_id": event["event_id"],
                "prediction_id": event["prediction_id"],
                "event_type": event["event_type"],
                "actor_id": event["actor_id"],
                "actor_role": event["actor_role"],
                "event_timestamp": event["event_timestamp"],
                "artifact_type": event["artifact_type"],
                "artifact_hash": event["artifact_hash"],
                "metadata_json": json.dumps(
                    event["metadata"],
                    sort_keys=True
                ),
                "previous_hash": event["previous_hash"]
            }

            if event["previous_hash"] != expected_previous_hash:

                return {
                    "valid": False,
                    "prediction_id": prediction_id,
                    "checked_events": len(events),
                    "failed_event_id": event["event_id"],
                    "error": "Previous hash mismatch"
                }

            calculated_hash = self.calculate_hash(record)

            if calculated_hash != event["record_hash"]:

                return {
                    "valid": False,
                    "prediction_id": prediction_id,
                    "checked_events": len(events),
                    "failed_event_id": event["event_id"],
                    "error": "Record hash mismatch"
                }

            expected_previous_hash = event["record_hash"]

        return {
            "valid": True,
            "prediction_id": prediction_id,
            "checked_events": len(events),
            "failed_event_id": None,
            "error": None
        }
