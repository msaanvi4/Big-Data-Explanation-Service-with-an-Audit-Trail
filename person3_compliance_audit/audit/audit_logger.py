
import hashlib
import json
import sqlite3
import uuid

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


# --------------------------------------------------
# DATABASE CONFIGURATION
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "audit_trail.db"

GENESIS_HASH = "0" * 64

VALID_ROLES = {
    "CUSTOMER",
    "REGULATOR",
    "ADMIN"
}

VALID_STATUSES = {
    "SUCCESS",
    "DENIED",
    "FAILED"
}


# --------------------------------------------------
# AUDIT LOGGER CLASS
# --------------------------------------------------

class AuditLogger:

    def __init__(self, db_path=None):
        """
        Initialize the audit logger.

        db_path:
            Optional custom SQLite database path.
            Useful for testing with temporary databases.
        """

        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH

        # Create parent directory if it does not exist
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Create audit table
        self.create_table()

    # --------------------------------------------------
    # DATABASE CONNECTION
    # --------------------------------------------------

    @contextmanager
    def get_connection(self):
        """
        Open a database connection and always close it.

        Commits successful operations and rolls back
        failed operations.
        """

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
    # CREATE AUDIT TABLE
    # --------------------------------------------------

    def create_table(self):
        """
        Create the audit_events table.

        Each record stores:
        - Who performed the action
        - User role
        - Customer reference
        - Prediction ID
        - Action performed
        - Purpose of access
        - Timestamp
        - Status
        - Reason
        - Previous record hash
        - Current record hash
        """

        with self.get_connection() as conn:

            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_events (

                    seq INTEGER PRIMARY KEY AUTOINCREMENT,

                    audit_id TEXT UNIQUE NOT NULL,

                    actor_id TEXT NOT NULL,

                    actor_role TEXT NOT NULL,

                    customer_ref TEXT,

                    prediction_id TEXT,

                    action TEXT NOT NULL,

                    purpose TEXT NOT NULL,

                    timestamp TEXT NOT NULL,

                    status TEXT NOT NULL,

                    reason TEXT,

                    previous_hash TEXT NOT NULL,

                    record_hash TEXT NOT NULL

                )
            """)

    # --------------------------------------------------
    # HASH GENERATION
    # --------------------------------------------------

    @staticmethod
    def calculate_hash(record):
        """
        Generate SHA-256 hash for an audit record.

        Sorting JSON keys ensures consistent hashing.
        """

        record_json = json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":")
        )

        return hashlib.sha256(
            record_json.encode("utf-8")
        ).hexdigest()

    # --------------------------------------------------
    # LOG AUDIT EVENT
    # --------------------------------------------------

    def log_event(
        self,
        actor_id,
        actor_role,
        action,
        purpose,
        customer_ref=None,
        prediction_id=None,
        status="SUCCESS",
        reason=None
    ):
        """
        Record an audit event.

        Supports:
        - Customer queries
        - Regulator queries
        - Admin actions
        - Successful access
        - Denied access
        - Failed operations

        Returns the inserted audit record.
        """

        actor_role = actor_role.upper()
        status = status.upper()

        # Validate role
        if actor_role not in VALID_ROLES:
            raise ValueError(
                f"Invalid actor role: {actor_role}"
            )

        # Validate status
        if status not in VALID_STATUSES:
            raise ValueError(
                f"Invalid status: {status}"
            )

        # Validate required fields
        if not actor_id or not str(actor_id).strip():
            raise ValueError("actor_id is required")

        if not action or not action.strip():
            raise ValueError("action is required")

        if not purpose or not purpose.strip():
            raise ValueError("purpose is required")

        audit_id = str(uuid.uuid4())

        timestamp = datetime.now(
            timezone.utc
        ).isoformat()

        with self.get_connection() as conn:

            # Lock database for this write transaction
            conn.execute("BEGIN IMMEDIATE")

            # Get previous record hash
            previous = conn.execute("""
                SELECT record_hash
                FROM audit_events
                ORDER BY seq DESC
                LIMIT 1
            """).fetchone()

            previous_hash = (
                previous["record_hash"]
                if previous
                else GENESIS_HASH
            )

            # Construct record
            record = {
                "audit_id": audit_id,
                "actor_id": str(actor_id),
                "actor_role": actor_role,
                "customer_ref": customer_ref,
                "prediction_id": prediction_id,
                "action": action,
                "purpose": purpose,
                "timestamp": timestamp,
                "status": status,
                "reason": reason,
                "previous_hash": previous_hash
            }

            # Generate current record hash
            record_hash = self.calculate_hash(record)

            # Insert record
            conn.execute("""
                INSERT INTO audit_events (
                    audit_id,
                    actor_id,
                    actor_role,
                    customer_ref,
                    prediction_id,
                    action,
                    purpose,
                    timestamp,
                    status,
                    reason,
                    previous_hash,
                    record_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                audit_id,
                str(actor_id),
                actor_role,
                customer_ref,
                prediction_id,
                action,
                purpose,
                timestamp,
                status,
                reason,
                previous_hash,
                record_hash
            ))

            record["record_hash"] = record_hash

            return record

    # --------------------------------------------------
    # RETRIEVE AUDIT EVENTS
    # --------------------------------------------------

    def get_events(self, customer_ref=None):
        """
        Retrieve audit events.

        If customer_ref is provided, retrieve only
        events associated with that customer.

        Note:
        This is a data retrieval method. Authorization
        must be checked separately before exposing results.
        """

        with self.get_connection() as conn:

            if customer_ref is None:

                rows = conn.execute("""
                    SELECT *
                    FROM audit_events
                    ORDER BY seq ASC
                """).fetchall()

            else:

                rows = conn.execute("""
                    SELECT *
                    FROM audit_events
                    WHERE customer_ref = ?
                    ORDER BY seq ASC
                """, (customer_ref,)).fetchall()

            return [
                dict(row)
                for row in rows
            ]

    # --------------------------------------------------
    # VERIFY HASH CHAIN
    # --------------------------------------------------

    def verify_chain(self):
        """
        Verify the integrity of the audit trail.

        Checks:
        1. Previous hash matches the preceding record.
        2. Each record hash matches its stored contents.

        Returns a verification report.
        """

        events = self.get_events()

        expected_previous_hash = GENESIS_HASH

        for event in events:

            # Reconstruct original record
            record = {
                "audit_id": event["audit_id"],
                "actor_id": event["actor_id"],
                "actor_role": event["actor_role"],
                "customer_ref": event["customer_ref"],
                "prediction_id": event["prediction_id"],
                "action": event["action"],
                "purpose": event["purpose"],
                "timestamp": event["timestamp"],
                "status": event["status"],
                "reason": event["reason"],
                "previous_hash": event["previous_hash"]
            }

            # Check previous hash
            if event["previous_hash"] != expected_previous_hash:

                return {
                    "valid": False,
                    "checked_records": len(events),
                    "failed_audit_id": event["audit_id"],
                    "error": "Previous hash mismatch"
                }

            # Recalculate current hash
            calculated_hash = self.calculate_hash(record)

            if calculated_hash != event["record_hash"]:

                return {
                    "valid": False,
                    "checked_records": len(events),
                    "failed_audit_id": event["audit_id"],
                    "error": "Record hash mismatch"
                }

            expected_previous_hash = event["record_hash"]

        return {
            "valid": True,
            "checked_records": len(events),
            "failed_audit_id": None,
            "error": None
        }


# --------------------------------------------------
# DIRECT EXECUTION TEST
# --------------------------------------------------

if __name__ == "__main__":

    logger = AuditLogger()

    event = logger.log_event(
        actor_id="customer_001",
        actor_role="CUSTOMER",
        customer_ref="customer_001",
        prediction_id="prediction_001",
        action="VIEW_EXPLANATION",
        purpose="Understand credit decision",
        status="SUCCESS"
    )

    print("\nAUDIT EVENT CREATED")
    print(json.dumps(event, indent=4))

    print("\nAUDIT TRAIL VERIFICATION")
    print(json.dumps(logger.verify_chain(), indent=4))
