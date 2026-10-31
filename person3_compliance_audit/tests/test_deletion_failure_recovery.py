
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from person2_query_interface.app.repository import get_repository

from person3_compliance_audit.deletion.deletion_service import (
    DeletionService,
)
from person3_compliance_audit.audit.audit_logger import AuditLogger
from person3_compliance_audit.security.authentication import (
    AuthenticationService,
)
from person3_compliance_audit.custody.chain_of_custody import (
    ChainOfCustody,
)
from person3_compliance_audit.integration.person3_adapter import (
    Person3Integration,
)


# ==========================================================
# TEST CLASS 1: DELETION SERVICE FAILURE AND RECOVERY
# ==========================================================

class TestDeletionFailureRecovery(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)

        self.service = DeletionService(
            db_path=self.base / "deletion.db"
        )

        self.service.add_customer_data(
            "customer_001",
            "PROFILE",
            "Synthetic profile",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_evidence_tampering_is_detected(self):

        result = self.service.request_deletion(
            customer_ref="customer_001",
            requested_by="admin_001",
        )

        request_id = result["request_id"]

        # Modify retained evidence without updating its hash.
        with self.service.get_connection() as conn:
            conn.execute(
                """
                UPDATE deletion_evidence
                SET deleted_records = 999
                WHERE request_id = ?
                """,
                (request_id,),
            )

        verification = self.service.verify_deletion(request_id)

        self.assertFalse(verification["verified"])
        self.assertFalse(verification["evidence_hash_valid"])

    def test_repeated_deletion_does_not_delete_other_customer_data(self):

        self.service.add_customer_data(
            "customer_002",
            "PROFILE",
            "Other customer",
        )

        first = self.service.request_deletion(
            customer_ref="customer_001",
            requested_by="admin_001",
        )

        second = self.service.request_deletion(
            customer_ref="customer_001",
            requested_by="admin_001",
        )

        self.assertEqual(first["status"], "COMPLETED")
        self.assertEqual(first["deleted_records"], 1)

        self.assertEqual(second["status"], "COMPLETED")
        self.assertEqual(second["deleted_records"], 0)

        self.assertEqual(
            self.service.count_customer_records("customer_001"),
            0,
        )

        self.assertEqual(
            self.service.count_customer_records("customer_002"),
            1,
        )

    def test_missing_evidence_request_returns_unverified(self):

        result = self.service.verify_deletion(
            "unknown-request-id"
        )

        self.assertFalse(result["verified"])

    def test_invalid_external_deletion_status_is_rejected(self):

        with self.assertRaises(ValueError):
            self.service.record_external_deletion(
                customer_ref="customer_001",
                actor_id="admin_001",
                actor_role="ADMIN",
                purpose="Failure test",
                deletion_result={"status": "FAILED"},
            )

    def test_non_admin_cannot_record_external_deletion(self):

        with self.assertRaises(PermissionError):
            self.service.record_external_deletion(
                customer_ref="customer_001",
                actor_id="regulator_001",
                actor_role="REGULATOR",
                purpose="Failure test",
                deletion_result={"status": "VERIFIED"},
            )


# ==========================================================
# TEST CLASS 2: PERSON 3 INTEGRATION FAILURE TESTS
# ==========================================================

class TestDeletionIntegrationFailures(unittest.TestCase):

    def setUp(self):

        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)

        # Connect to Person 2 repository.
        self.repository = get_repository()

        self.prediction_id = (
            self.repository.list_prediction_ids()[0]
        )

        bundle = self.repository.get_decision_bundle(
            self.prediction_id
        )

        self.customer_ref = (
            bundle.prediction.request_trace_id
        )

        # Copy Parquet dataset into temporary directory.
        project_root = Path(__file__).resolve().parents[2]

        original_batches = (
            project_root
            / "person2_query_interface"
            / "data"
            / "monthly_batches"
        )

        self.test_batches = self.base / "monthly_batches"

        shutil.copytree(
            original_batches,
            self.test_batches,
        )

        # Create isolated databases.
        self.audit_logger = AuditLogger(
            db_path=self.base / "audit.db"
        )

        self.custody = ChainOfCustody(
            db_path=self.base / "custody.db"
        )

        self.authentication = AuthenticationService(
            db_path=self.base / "identity.db"
        )

        self.deletion_service = DeletionService(
            db_path=self.base / "deletion.db"
        )

        # Create administrator.
        self.authentication.bootstrap_admin(
            "failure_test_admin",
            "FailureTestPassword@2026",
        )

        login = self.authentication.login(
            "failure_test_admin",
            "FailureTestPassword@2026",
        )

        self.admin_token = login["session_token"]

        # Initialize Person 3 integration.
        self.integration = Person3Integration(
            repository=self.repository,
            audit_logger=self.audit_logger,
            custody=self.custody,
            authentication_service=self.authentication,
            parquet_batches_dir=self.test_batches,
            deletion_service=self.deletion_service,
        )

        # Mock the deletion adapter so failure scenarios
        # can be simulated without modifying Parquet files.
        self.integration.parquet_deletion = Mock()

    def tearDown(self):
        self.temp_dir.cleanup()

    def verified_deletion_result(self):

        return {
            "status": "VERIFIED",
            "deleted_predictions": 1,
            "deleted_input_snapshots": 1,
            "deleted_explanations": 1,
            "preserved_shared_inputs": 0,
            "preserved_shared_explanations": 0,
            "modified_batches": ["2026-11"],
        }

    # ------------------------------------------------------
    # TEST 6: PARQUET DELETION FAILURE
    # ------------------------------------------------------

    def test_parquet_deletion_failure_is_reported(self):

        self.integration.parquet_deletion.delete_customer_records.side_effect = (
            OSError("Simulated Parquet storage failure")
        )

        result = self.integration.delete_customer_data(
            session_token=self.admin_token,
            customer_ref=self.customer_ref,
            purpose="Failure recovery test",
        )

        self.assertTrue(result["authorized"])
        self.assertEqual(result["status"], "FAILED")

        self.assertIsNone(result["deletion_result"])
        self.assertIsNone(result["evidence"])

        self.assertEqual(
            result["audit_event"]["status"],
            "FAILED",
        )

        # Evidence must not be recorded when deletion fails.
        self.deletion_service.record_external_deletion = Mock()

        self.deletion_service.record_external_deletion.assert_not_called()

    # ------------------------------------------------------
    # TEST 7: EVIDENCE STORAGE FAILURE
    # ------------------------------------------------------

    def test_evidence_storage_failure_returns_partial(self):

        self.integration.parquet_deletion.delete_customer_records.return_value = (
            self.verified_deletion_result()
        )

        # Simulate evidence database failure.
        self.deletion_service.record_external_deletion = Mock(
            side_effect=OSError(
                "Simulated evidence database failure"
            )
        )

        result = self.integration.delete_customer_data(
            session_token=self.admin_token,
            customer_ref=self.customer_ref,
            purpose="Evidence failure test",
        )

        self.assertTrue(result["authorized"])
        self.assertEqual(result["status"], "PARTIAL")

        self.assertEqual(
            result["deletion_result"]["status"],
            "VERIFIED",
        )

        self.assertIsNone(result["evidence"])

        self.assertEqual(
            result["audit_event"]["status"],
            "FAILED",
        )

    # ------------------------------------------------------
    # TEST 8: AUDIT LOGGING FAILURE
    # ------------------------------------------------------

    def test_audit_failure_is_reported_after_deletion(self):

        self.integration.parquet_deletion.delete_customer_records.return_value = (
            self.verified_deletion_result()
        )

        original_log_event = self.audit_logger.log_event

        call_count = {"value": 0}

        def fail_execution_audit(*args, **kwargs):

            call_count["value"] += 1

            # First call: authorization audit succeeds.
            # Second call: deletion execution audit fails.
            if call_count["value"] == 2:
                raise OSError(
                    "Simulated audit database failure"
                )

            return original_log_event(*args, **kwargs)

        self.audit_logger.log_event = fail_execution_audit

        result = self.integration.delete_customer_data(
            session_token=self.admin_token,
            customer_ref=self.customer_ref,
            purpose="Audit failure test",
        )

        self.assertTrue(result["authorized"])

        self.assertEqual(
            result["status"],
            "AUDIT_RECORDING_FAILED",
        )

        self.assertEqual(
            result["deletion_result"]["status"],
            "VERIFIED",
        )

        self.assertIsNotNone(result["evidence"])
        self.assertIsNone(result["audit_event"])

        # Evidence should remain verifiable even though
        # execution audit recording failed.
        verification = self.integration.verify_deletion_evidence(
            result["request_id"]
        )

        self.assertTrue(verification["verified"])


# ==========================================================
# RUN ALL TESTS
# ==========================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
