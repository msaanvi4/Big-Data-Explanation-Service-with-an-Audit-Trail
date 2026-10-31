
import tempfile
import unittest
from pathlib import Path

from audit.audit_logger import AuditLogger


class TestAuditLogger(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "test_audit.db"
        self.logger = AuditLogger(db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_customer_event_logging(self):
        event = self.logger.log_event(
            actor_id="USER001",
            actor_role="CUSTOMER",
            customer_ref="CUST001",
            prediction_id="PRED001",
            action="VIEW_EXPLANATION",
            purpose="CUSTOMER_QUERY",
            status="SUCCESS"
        )

        self.assertEqual(event["actor_role"], "CUSTOMER")
        self.assertEqual(event["status"], "SUCCESS")
        self.assertEqual(len(event["record_hash"]), 64)

    def test_regulator_event_logging(self):
        self.logger.log_event(
            actor_id="REG001",
            actor_role="REGULATOR",
            customer_ref="CUST001",
            prediction_id="PRED001",
            action="VIEW_EXPLANATION",
            purpose="REGULATORY_AUDIT",
            status="SUCCESS"
        )

        self.assertTrue(
            self.logger.verify_chain()["valid"]
        )

    def test_hash_chain_integrity(self):
        for i in range(3):
            self.logger.log_event(
                actor_id="USER001",
                actor_role="CUSTOMER",
                customer_ref="CUST001",
                prediction_id=f"PRED00{i}",
                action="VIEW_EXPLANATION",
                purpose="CUSTOMER_QUERY",
                status="SUCCESS"
            )

        result = self.logger.verify_chain()

        self.assertTrue(result["valid"])
        self.assertEqual(result["checked_records"], 3)

    def test_tampering_detection(self):
        event = self.logger.log_event(
            actor_id="USER001",
            actor_role="CUSTOMER",
            customer_ref="CUST001",
            prediction_id="PRED001",
            action="VIEW_EXPLANATION",
            purpose="CUSTOMER_QUERY",
            status="SUCCESS"
        )

        with self.logger.get_connection() as conn:
            conn.execute("""
                UPDATE audit_events
                SET status = 'DENIED'
                WHERE audit_id = ?
            """, (event["audit_id"],))

        result = self.logger.verify_chain()

        self.assertFalse(result["valid"])

    def test_invalid_role_rejected(self):
        with self.assertRaises(ValueError):
            self.logger.log_event(
                actor_id="USER001",
                actor_role="UNKNOWN",
                customer_ref="CUST001",
                prediction_id="PRED001",
                action="VIEW_EXPLANATION",
                purpose="CUSTOMER_QUERY",
                status="SUCCESS"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
