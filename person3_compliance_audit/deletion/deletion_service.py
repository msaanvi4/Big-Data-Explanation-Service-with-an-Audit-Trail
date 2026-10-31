
"""
Person 3: Right-to-be-Forgotten Deletion Service

Supports:
1. Synthetic SQLite customer-data deletion.
2. Retention of minimal deletion evidence.
3. External Parquet deletion evidence.
4. Evidence hash verification.
5. Integration with the Person 3 audit logger.

This is a prototype, not a legal determination of
which records must be retained under applicable law.

The original customer reference is never stored in
the retained deletion-evidence table.
"""

import hashlib
import json
import sqlite3
import uuid

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_DB_PATH = (
    Path(__file__).resolve().parent / "deletion_store.db"
)


class DeletionService:

    def __init__(self, db_path=None, audit_logger=None):

        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.audit_logger = audit_logger

        self.create_tables()

    # --------------------------------------------------
    # DATABASE CONNECTION
    # --------------------------------------------------

    @contextmanager
    def get_connection(self):

        conn = sqlite3.connect(
            self.db_path,
            timeout=30,
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
    # TIME AND HASH HELPERS
    # --------------------------------------------------

    @staticmethod
    def _now():

        return (
            datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _calculate_evidence_hash(evidence):

        canonical_data = json.dumps(
            evidence,
            sort_keys=True,
            separators=(",", ":"),
        )

        return hashlib.sha256(
            canonical_data.encode("utf-8")
        ).hexdigest()

    @staticmethod
    def generate_subject_token(customer_ref, request_id):
        """
        Generate a request-specific pseudonymous token.

        This is pseudonymization, not guaranteed anonymization.
        """

        value = f"{request_id}:{customer_ref}"

        return hashlib.sha256(
            value.encode("utf-8")
        ).hexdigest()

    # --------------------------------------------------
    # CREATE TABLES AND MIGRATIONS
    # --------------------------------------------------

    def create_tables(self):

        with self.get_connection() as conn:

            conn.execute("""
                CREATE TABLE IF NOT EXISTS customer_data (
                    record_id TEXT PRIMARY KEY,
                    customer_ref TEXT NOT NULL,
                    data_type TEXT NOT NULL,
                    data_value TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_customer_data_ref
                ON customer_data(customer_ref)
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS deletion_evidence (
                    request_id TEXT PRIMARY KEY,
                    subject_token TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    deleted_records INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL
                )
            """)

            # Backward-compatible schema migrations.
            existing_columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(deletion_evidence)"
                ).fetchall()
            }

            migrations = {
                "backend": (
                    "ALTER TABLE deletion_evidence "
                    "ADD COLUMN backend TEXT NOT NULL DEFAULT 'SQLITE'"
                ),
                "details_json": (
                    "ALTER TABLE deletion_evidence "
                    "ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}'"
                ),
                "actor_id": (
                    "ALTER TABLE deletion_evidence "
                    "ADD COLUMN actor_id TEXT"
                ),
            }

            for column, sql in migrations.items():

                if column not in existing_columns:
                    conn.execute(sql)

    # --------------------------------------------------
    # ADD SYNTHETIC CUSTOMER DATA
    # --------------------------------------------------

    def add_customer_data(
        self,
        customer_ref,
        data_type,
        data_value,
    ):
        """
        Add synthetic customer-linked data.

        Intended for prototype setup and testing.
        """

        if not customer_ref:
            raise ValueError("customer_ref is required")

        if not data_type:
            raise ValueError("data_type is required")

        if data_value is None:
            raise ValueError("data_value is required")

        record_id = str(uuid.uuid4())

        created_at = self._now()

        with self.get_connection() as conn:

            conn.execute("""
                INSERT INTO customer_data (
                    record_id,
                    customer_ref,
                    data_type,
                    data_value,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                record_id,
                customer_ref,
                data_type,
                str(data_value),
                created_at,
            ))

        return record_id

    # --------------------------------------------------
    # COUNT CUSTOMER RECORDS
    # --------------------------------------------------

    def count_customer_records(self, customer_ref):

        with self.get_connection() as conn:

            row = conn.execute("""
                SELECT COUNT(*) AS total
                FROM customer_data
                WHERE customer_ref = ?
            """, (customer_ref,)).fetchone()

            return row["total"]

    # --------------------------------------------------
    # BUILD EVIDENCE
    # --------------------------------------------------

    def _build_evidence(
        self,
        request_id,
        subject_token,
        requested_at,
        completed_at,
        deleted_records,
        status,
        backend,
        details,
    ):

        evidence = {
            "request_id": request_id,
            "subject_token": subject_token,
            "requested_at": requested_at,
            "completed_at": completed_at,
            "deleted_records": int(deleted_records),
            "status": status,
            "backend": backend,
            "details": details,
        }

        return evidence

    # --------------------------------------------------
    # STORE RETAINED EVIDENCE
    # --------------------------------------------------

    def _store_evidence(
        self,
        request_id,
        subject_token,
        requested_at,
        completed_at,
        deleted_records,
        status,
        backend,
        details,
        actor_id=None,
    ):

        evidence = self._build_evidence(
            request_id=request_id,
            subject_token=subject_token,
            requested_at=requested_at,
            completed_at=completed_at,
            deleted_records=deleted_records,
            status=status,
            backend=backend,
            details=details,
        )

        evidence_hash = self._calculate_evidence_hash(
            evidence
        )

        with self.get_connection() as conn:

            conn.execute("""
                INSERT INTO deletion_evidence (
                    request_id,
                    subject_token,
                    requested_at,
                    completed_at,
                    deleted_records,
                    status,
                    evidence_hash,
                    backend,
                    details_json,
                    actor_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                request_id,
                subject_token,
                requested_at,
                completed_at,
                deleted_records,
                status,
                evidence_hash,
                backend,
                json.dumps(details, sort_keys=True),
                actor_id,
            ))

        return {
            "request_id": request_id,
            "status": status,
            "backend": backend,
            "deleted_records": deleted_records,
            "completed_at": completed_at,
            "evidence_hash": evidence_hash,
        }

    # --------------------------------------------------
    # LEGACY SQLITE DELETION
    # --------------------------------------------------

    def request_deletion(
        self,
        customer_ref,
        requested_by,
        purpose="Customer right-to-be-forgotten request",
    ):
        """
        Delete synthetic SQLite customer data.

        Compatibility method for the existing SQLite prototype.

        IMPORTANT:
        The caller must authenticate and authorize the requester
        before calling this method.

        Do not use this method as the production authentication
        boundary. Person3Integration handles authenticated
        Parquet deletion separately.
        """

        if not customer_ref:
            raise ValueError("customer_ref is required")

        if not requested_by:
            raise ValueError("requested_by is required")

        request_id = str(uuid.uuid4())

        requested_at = self._now()

        subject_token = self.generate_subject_token(
            customer_ref,
            request_id,
        )

        # Delete data and retain evidence in one SQLite transaction.
        with self.get_connection() as conn:

            row = conn.execute("""
                SELECT COUNT(*) AS total
                FROM customer_data
                WHERE customer_ref = ?
            """, (customer_ref,)).fetchone()

            deleted_records = row["total"]

            conn.execute("""
                DELETE FROM customer_data
                WHERE customer_ref = ?
            """, (customer_ref,))

            # Verify the actual SQLite customer records are gone.
            remaining = conn.execute("""
                SELECT COUNT(*) AS total
                FROM customer_data
                WHERE customer_ref = ?
            """, (customer_ref,)).fetchone()["total"]

            if remaining != 0:
                raise RuntimeError(
                    "SQLite deletion verification failed."
                )

            completed_at = self._now()

            status = "COMPLETED"

            evidence = self._build_evidence(
                request_id=request_id,
                subject_token=subject_token,
                requested_at=requested_at,
                completed_at=completed_at,
                deleted_records=deleted_records,
                status=status,
                backend="SQLITE",
                details={
                    "verification": "NO_MATCHING_CUSTOMER_RECORDS",
                },
            )

            evidence_hash = self._calculate_evidence_hash(
                evidence
            )

            conn.execute("""
                INSERT INTO deletion_evidence (
                    request_id,
                    subject_token,
                    requested_at,
                    completed_at,
                    deleted_records,
                    status,
                    evidence_hash,
                    backend,
                    details_json,
                    actor_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                request_id,
                subject_token,
                requested_at,
                completed_at,
                deleted_records,
                status,
                evidence_hash,
                "SQLITE",
                json.dumps(
                    evidence["details"],
                    sort_keys=True,
                ),
                str(requested_by),
            ))

        # Backward compatibility for existing prototype tests.
        # Production role and identity must come from authentication.
        if self.audit_logger:

            self.audit_logger.log_event(
                actor_id=str(requested_by),
                actor_role="ADMIN",
                customer_ref=None,
                prediction_id=None,
                action="DELETE_CUSTOMER_DATA",
                purpose=purpose,
                status="SUCCESS",
                reason=(
                    f"SQLite deletion completed; "
                    f"records deleted: {deleted_records}; "
                    f"request ID: {request_id}"
                ),
            )

        return {
            "request_id": request_id,
            "status": status,
            "deleted_records": deleted_records,
            "completed_at": completed_at,
            "evidence_hash": evidence_hash,
        }

    # --------------------------------------------------
    # RECORD EXTERNAL PARQUET DELETION EVIDENCE
    # --------------------------------------------------

    def record_external_deletion(
        self,
        customer_ref,
        actor_id,
        actor_role,
        purpose,
        deletion_result,
        request_id=None,
    ):
        """
        Retain evidence for a deletion performed by an external
        storage adapter, such as ParquetDeletionAdapter.

        This method does not delete Parquet data itself.

        The adapter must complete and verify deletion first.

        The original customer reference is used only to generate
        the request-specific token. It is not stored in evidence.
        """

        if not customer_ref:
            raise ValueError("customer_ref is required")

        if not actor_id:
            raise ValueError("actor_id is required")

        if actor_role != "ADMIN":
            raise PermissionError(
                "Only an authenticated ADMIN may record "
                "an administrator deletion operation."
            )

        if not purpose:
            raise ValueError("purpose is required")

        if not isinstance(deletion_result, dict):
            raise ValueError(
                "deletion_result must be a dictionary."
            )

        result_status = deletion_result.get("status")

        if result_status not in {"VERIFIED", "NOT_FOUND"}:
            raise ValueError(
                "Only verified deletion results or NOT_FOUND "
                "results can be retained as completed evidence."
            )

        request_id = request_id or str(uuid.uuid4())

        requested_at = self._now()
        completed_at = self._now()

        subject_token = self.generate_subject_token(
            customer_ref,
            request_id,
        )

        deleted_predictions = int(
            deletion_result.get("deleted_predictions", 0)
        )

        deleted_inputs = int(
            deletion_result.get("deleted_input_snapshots", 0)
        )

        deleted_explanations = int(
            deletion_result.get("deleted_explanations", 0)
        )

        details = {
            "purpose": purpose,
            "predictions_deleted": deleted_predictions,
            "input_snapshots_deleted": deleted_inputs,
            "explanations_deleted": deleted_explanations,
            "preserved_shared_inputs": int(
                deletion_result.get(
                    "preserved_shared_inputs", 0
                )
            ),
            "preserved_shared_explanations": int(
                deletion_result.get(
                    "preserved_shared_explanations", 0
                )
            ),
            "modified_batches": sorted(
                deletion_result.get("modified_batches", [])
            ),
            "adapter_status": result_status,
        }

        evidence_status = "COMPLETED"

        total_deleted = (
            deleted_predictions
            + deleted_inputs
            + deleted_explanations
        )

        return self._store_evidence(
            request_id=request_id,
            subject_token=subject_token,
            requested_at=requested_at,
            completed_at=completed_at,
            deleted_records=total_deleted,
            status=evidence_status,
            backend="PARQUET",
            details=details,
            actor_id=str(actor_id),
        )

    # --------------------------------------------------
    # VERIFY RETAINED DELETION EVIDENCE
    # --------------------------------------------------

    def verify_deletion(self, request_id):
        """
        Verify the retained evidence record.

        This verifies evidence integrity and recorded completion.
        It does not independently inspect historical backups,
        Iceberg snapshots, or external storage.
        """

        with self.get_connection() as conn:

            evidence = conn.execute("""
                SELECT *
                FROM deletion_evidence
                WHERE request_id = ?
            """, (request_id,)).fetchone()

            if evidence is None:

                return {
                    "verified": False,
                    "reason": "Deletion request not found",
                }

            try:
                details = json.loads(
                    evidence["details_json"]
                )
            except (ValueError, TypeError):

                return {
                    "verified": False,
                    "reason": "Evidence details are invalid",
                }

            evidence_data = self._build_evidence(
                request_id=evidence["request_id"],
                subject_token=evidence["subject_token"],
                requested_at=evidence["requested_at"],
                completed_at=evidence["completed_at"],
                deleted_records=evidence["deleted_records"],
                status=evidence["status"],
                backend=evidence["backend"],
                details=details,
            )

            calculated_hash = self._calculate_evidence_hash(
                evidence_data
            )

            hash_valid = (
                calculated_hash == evidence["evidence_hash"]
            )

            status_valid = (
                evidence["status"] == "COMPLETED"
            )

            return {
                "verified": hash_valid and status_valid,
                "status": evidence["status"],
                "backend": evidence["backend"],
                "deleted_records": evidence["deleted_records"],
                "evidence_hash_valid": hash_valid,
                "request_id": evidence["request_id"],
                "reason": (
                    "Evidence integrity verified."
                    if hash_valid and status_valid
                    else "Evidence integrity or completion status invalid."
                ),
            }

    # --------------------------------------------------
    # RETRIEVE RETAINED DELETION EVIDENCE
    # --------------------------------------------------

    def get_deletion_evidence(self, request_id):

        with self.get_connection() as conn:

            row = conn.execute("""
                SELECT *
                FROM deletion_evidence
                WHERE request_id = ?
            """, (request_id,)).fetchone()

            return dict(row) if row else None
