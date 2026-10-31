
import tempfile
import unittest

from pathlib import Path

from audit.audit_logger import AuditLogger
from security.authorization import AuthorizationService


class TestAuthorization(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

        db_path = Path(self.temp_dir.name) / "authorization_test.db"

        self.logger = AuditLogger(db_path)
        self.auth = AuthorizationService(self.logger)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_customer_can_access_own_record(self):

        result = self.auth.authorize(
            actor_id="customer_001",
            actor_role="CUSTOMER",
            action="VIEW_EXPLANATION",
            purpose="Understand my credit decision",
            customer_ref="customer_001",
            prediction_id="prediction_001"
        )

        self.assertTrue(result["authorized"])

    def test_customer_cannot_access_another_record(self):

        result = self.auth.authorize(
            actor_id="customer_001",
            actor_role="CUSTOMER",
            action="VIEW_EXPLANATION",
            purpose="View another customer's decision",
            customer_ref="customer_002",
            prediction_id="prediction_002"
        )

        self.assertFalse(result["authorized"])

    def test_regulator_can_access_records(self):

        result = self.auth.authorize(
            actor_id="regulator_001",
            actor_role="REGULATOR",
            action="VIEW_DECISION",
            purpose="Regulatory investigation",
            customer_ref="customer_002",
            prediction_id="prediction_002"
        )

        self.assertTrue(result["authorized"])

    def test_customer_cannot_export_compliance_report(self):

        result = self.auth.authorize(
            actor_id="customer_001",
            actor_role="CUSTOMER",
            action="EXPORT_COMPLIANCE_REPORT",
            purpose="Download compliance report",
            customer_ref="customer_001"
        )

        self.assertFalse(result["authorized"])

    def test_missing_purpose_is_denied_and_logged(self):

        result = self.auth.authorize(
            actor_id="customer_001",
            actor_role="CUSTOMER",
            action="VIEW_DECISION",
            purpose="",
            customer_ref="customer_001"
        )

        self.assertFalse(result["authorized"])
        self.assertEqual(
            result["audit_event"]["status"],
            "DENIED"
        )

    def test_denied_access_is_recorded_in_audit_trail(self):

        self.auth.authorize(
            actor_id="customer_001",
            actor_role="CUSTOMER",
            action="VIEW_EXPLANATION",
            purpose="Unauthorized access attempt",
            customer_ref="customer_002"
        )

        events = self.logger.get_events()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "DENIED")

    def test_admin_can_perform_administrative_action(self):

        result = self.auth.authorize(
            actor_id="admin_001",
            actor_role="ADMIN",
            action="DELETE_RECORD",
            purpose="Approved administrative operation",
            customer_ref="customer_001"
        )

        self.assertTrue(result["authorized"])

    def test_invalid_role_is_rejected(self):

        with self.assertRaises(ValueError):

            self.auth.authorize(
                actor_id="user_001",
                actor_role="UNKNOWN",
                action="VIEW_DECISION",
                purpose="Testing invalid role",
                customer_ref="user_001"
            )

    def test_authorization_events_preserve_hash_chain(self):

        self.auth.authorize(
            actor_id="customer_001",
            actor_role="CUSTOMER",
            action="VIEW_DECISION",
            purpose="View own decision",
            customer_ref="customer_001"
        )

        self.auth.authorize(
            actor_id="customer_001",
            actor_role="CUSTOMER",
            action="VIEW_DECISION",
            purpose="Attempt to view another decision",
            customer_ref="customer_002"
        )

        result = self.logger.verify_chain()

        self.assertTrue(result["valid"])
        self.assertEqual(result["checked_records"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
