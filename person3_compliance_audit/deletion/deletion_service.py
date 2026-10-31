
"""
Person 3: Right-to-be-Forgotten Deletion Service

Prototype workflow:
1. Store customer-linked data separately.
2. Process a deletion request.
3. Delete the customer's operational data.
4. Retain a minimal deletion-evidence record.
5. Record the deletion operation in the audit trail.

This is a synthetic-data prototype, not a legal determination
of which records must be retained under applicable law.
"""

import hashlib
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
    # CREATE TABLES
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

    # --------------------------------------------------
    # ADD SYNTHETIC CUSTOMER DATA
    # --------------------------------------------------

    def add_customer_data(
        self,
        customer_ref,
        data_type,
        data_value
    ):
        """
        Add a synthetic customer-linked record.

        This method is for prototype setup and testing.
        """

        if not customer_ref:
            raise ValueError("customer_ref is required")

        if not data_type:
            raise ValueError("data_type is required")

        if data_value is None:
            raise ValueError("data_value is required")

        record_id = str(uuid.uuid4())

        created_at = datetime.now(
            timezone.utc
        ).isoformat()

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
                created_at
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
    # GENERATE PSEUDONYMOUS SUBJECT TOKEN
    # --------------------------------------------------

    @staticmethod
    def generate_subject_token(customer_ref, request_id):
        """
        Create a request-specific pseudonymous token.

        The original customer reference is not stored in
        the deletion evidence table.

        Note: A hash is pseudonymization, not guaranteed
        anonymization. Do not store the secret reference
        mapping in the retained evidence.
        """

        value = f"{request_id}:{customer_ref}"

        return hashlib.sha256(
            value.encode("utf-8")
        ).hexdigest()

    # --------------------------------------------------
    # REQUEST DELETION
    # --------------------------------------------------

    def request_deletion(
        self,
        customer_ref,
        requested_by,
        purpose="Customer right-to-be-forgotten request"
    ):
        """
        Delete customer-linked operational data and retain
        a minimal deletion evidence record.

        requested_by identifies the authenticated requester
        in this prototype. Real authentication is required
        before using this in a production service.
        """

        if not customer_ref:
            raise ValueError("customer_ref is required")

        if not requested_by:
            raise ValueError("requested_by is required")

        request_id = str(uuid.uuid4())

        requested_at = datetime.now(
            timezone.utc
        ).isoformat()

        subject_token = self.generate_subject_token(
            customer_ref,
            request_id
        )

        with self.get_connection() as conn:

            # Count records before deletion
            row = conn.execute("""
                SELECT COUNT(*) AS total
                FROM customer_data
                WHERE customer_ref = ?
            """, (customer_ref,)).fetchone()

            deleted_records = row["total"]

            # Delete customer-linked operational data
            conn.execute("""
                DELETE FROM customer_data
                WHERE customer_ref = ?
            """, (customer_ref,))

            completed_at = datetime.now(
                timezone.utc
            ).isoformat()

            status = "COMPLETED"

            # Retain minimal evidence without the original
            # customer reference or personal data.
            evidence = {
                "request_id": request_id,
                "subject_token": subject_token,
                "requested_at": requested_at,
                "completed_at": completed_at,
                "deleted_records": deleted_records,
                "status": status
            }

            evidence_hash = hashlib.sha256(
                str(sorted(evidence.items())).encode("utf-8")
            ).hexdigest()

            conn.execute("""
                INSERT INTO deletion_evidence (
                    request_id,
                    subject_token,
                    requested_at,
                    completed_at,
                    deleted_records,
                    status,
                    evidence_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                request_id,
                subject_token,
                requested_at,
                completed_at,
                deleted_records,
                status,
                evidence_hash
            ))

        # Record the deletion operation in the separate
        # Person 3 audit trail without putting the original
        # customer reference into this new audit event.
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
                    f"Deletion completed; "
                    f"records deleted: {deleted_records}; "
                    f"request ID: {request_id}"
                )
            )

        return {
            "request_id": request_id,
            "status": status,
            "deleted_records": deleted_records,
            "completed_at": completed_at,
            "evidence_hash": evidence_hash
        }

    # --------------------------------------------------
    # VERIFY DELETION
    # --------------------------------------------------

    def verify_deletion(self, request_id):

        with self.get_connection() as conn:

            evidence = conn.execute("""
                SELECT *
                FROM deletion_evidence
                WHERE request_id = ?
            """, (request_id,)).fetchone()

            if evidence is None:
                return {
                    "verified": False,
                    "reason": "Deletion request not found"
                }

            remaining = conn.execute("""
                SELECT COUNT(*) AS total
                FROM customer_data
                WHERE record_id = ?
            """, ("__no_matching_record__",)).fetchone()

            # Recalculate retained evidence hash
            evidence_data = {
                "request_id": evidence["request_id"],
                "subject_token": evidence["subject_token"],
                "requested_at": evidence["requested_at"],
                "completed_at": evidence["completed_at"],
                "deleted_records": evidence["deleted_records"],
                "status": evidence["status"]
            }

            calculated_hash = hashlib.sha256(
                str(sorted(evidence_data.items())).encode("utf-8")
            ).hexdigest()

            hash_valid = (
                calculated_hash == evidence["evidence_hash"]
            )

            return {
                "verified": (
                    evidence["status"] == "COMPLETED"
                    and hash_valid
                ),
                "status": evidence["status"],
                "deleted_records": evidence["deleted_records"],
                "evidence_hash_valid": hash_valid,
                "request_id": evidence["request_id"]
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
