
import tempfile
import unittest

from pathlib import Path

from audit.audit_logger import AuditLogger
from deletion.deletion_service import DeletionService


class TestDeletionService(unittest.TestCase):

    def setUp(self):

        self.temp_dir = tempfile.TemporaryDirectory()

        base = Path(self.temp_dir.name)

        self.logger = AuditLogger(
            base / "audit_test.db"
        )

        self.service = DeletionService(
            db_path=base / "deletion_test.db",
            audit_logger=self.logger
        )

        # Add synthetic records for two customers
        self.service.add_customer_data(
            "customer_001",
            "PROFILE",
            "Synthetic profile data"
        )

        self.service.add_customer_data(
            "customer_001",
            "PREDICTION_INPUT",
            "Synthetic prediction input"
        )

        self.service.add_customer_data(
            "customer_002",
            "PROFILE",
            "Another synthetic profile"
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_customer_data_is_added(self):

        count = self.service.count_customer_records(
            "customer_001"
        )

        self.assertEqual(count, 2)

    def test_deletion_removes_customer_records(self):

        result = self.service.request_deletion(
            customer_ref="customer_001",
            requested_by="admin_001"
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["deleted_records"], 2)

        remaining = self.service.count_customer_records(
            "customer_001"
        )

        self.assertEqual(remaining, 0)

    def test_other_customer_data_is_preserved(self):

        self.service.request_deletion(
            customer_ref="customer_001",
            requested_by="admin_001"
        )

        remaining = self.service.count_customer_records(
            "customer_002"
        )

        self.assertEqual(remaining, 1)

    def test_deletion_evidence_is_retained(self):

        result = self.service.request_deletion(
            customer_ref="customer_001",
            requested_by="admin_001"
        )

        evidence = self.service.get_deletion_evidence(
            result["request_id"]
        )

        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["status"], "COMPLETED")

        # The retained evidence must not contain the
        # original customer reference.
        self.assertNotIn(
            "customer_001",
            str(evidence)
        )

    def test_deletion_evidence_hash_verification(self):

        result = self.service.request_deletion(
            customer_ref="customer_001",
            requested_by="admin_001"
        )

        verification = self.service.verify_deletion(
            result["request_id"]
        )

        self.assertTrue(verification["verified"])
        self.assertTrue(
            verification["evidence_hash_valid"]
        )

    def test_deletion_is_logged_in_audit_trail(self):

        self.service.request_deletion(
            customer_ref="customer_001",
            requested_by="admin_001"
        )

        events = self.logger.get_events()

        self.assertEqual(len(events), 1)

        self.assertEqual(
            events[0]["action"],
            "DELETE_CUSTOMER_DATA"
        )

        self.assertEqual(
            events[0]["actor_role"],
            "ADMIN"
        )

        self.assertIsNone(
            events[0]["customer_ref"]
        )

    def test_deletion_audit_chain_remains_valid(self):

        self.service.request_deletion(
            customer_ref="customer_001",
            requested_by="admin_001"
        )

        result = self.logger.verify_chain()

        self.assertTrue(result["valid"])

    def test_missing_customer_reference_is_rejected(self):

        with self.assertRaises(ValueError):

            self.service.request_deletion(
                customer_ref="",
                requested_by="admin_001"
            )

    def test_missing_requester_is_rejected(self):

        with self.assertRaises(ValueError):

            self.service.request_deletion(
                customer_ref="customer_001",
                requested_by=""
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
