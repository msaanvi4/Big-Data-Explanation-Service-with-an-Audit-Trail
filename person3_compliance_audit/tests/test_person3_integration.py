
"""
Person 3 Integration Tests

Tests the integration of:
1. Person 2 DuckDB/Parquet repository
2. Person 3 authorization
3. Person 3 audit logging
4. Person 3 chain of custody
5. Person 2 reproducibility engine
"""

import unittest
import tempfile
from pathlib import Path

from person2_query_interface.app.repository import get_repository

from person3_compliance_audit.audit.audit_logger import AuditLogger
from person3_compliance_audit.custody.chain_of_custody import ChainOfCustody
from person3_compliance_audit.integration.person3_adapter import (
    Person3Integration
)


class TestPerson3Integration(unittest.TestCase):

    def setUp(self):
        """Create isolated databases and connect to Person 2."""

        self.temp_dir = tempfile.TemporaryDirectory()
        db_folder = Path(self.temp_dir.name)

        # Use the actual Person 2 DuckDB/Parquet repository.
        self.repository = get_repository()

        # Select a real synthetic prediction.
        self.prediction_id = self.repository.list_prediction_ids()[0]

        self.bundle = self.repository.get_decision_bundle(
            self.prediction_id
        )

        self.customer_ref = self.bundle.prediction.request_trace_id

        # Create isolated Person 3 databases.
        self.audit_logger = AuditLogger(
            db_path=str(db_folder / "audit.db")
        )

        self.custody = ChainOfCustody(
            db_path=str(db_folder / "custody.db")
        )

        # Initialize the complete integration.
        self.integration = Person3Integration(
            repository=self.repository,
            audit_logger=self.audit_logger,
            custody=self.custody
        )

    def tearDown(self):
        """Clean up temporary databases."""

        self.temp_dir.cleanup()

    def test_customer_can_access_own_prediction(self):

        result = self.integration.access_prediction(
            actor_id=self.customer_ref,
            actor_role="CUSTOMER",
            prediction_id=self.prediction_id,
            purpose="Customer decision review"
        )

        self.assertTrue(result["authorized"])
        self.assertEqual(result["audit_event"]["status"], "SUCCESS")
        self.assertEqual(
            result["custody_event"]["event_type"],
            "PREDICTION_ACCESSED"
        )

        verification = self.integration.verify_custody(
            self.prediction_id
        )

        self.assertTrue(verification["valid"])

    def test_customer_cannot_access_another_customer_prediction(self):

        result = self.integration.access_prediction(
            actor_id="different-synthetic-customer",
            actor_role="CUSTOMER",
            prediction_id=self.prediction_id,
            purpose="Unauthorized access attempt"
        )

        self.assertFalse(result["authorized"])
        self.assertEqual(result["audit_event"]["status"], "DENIED")

    def test_regulator_can_access_prediction(self):

        result = self.integration.access_prediction(
            actor_id="regulator-demo-01",
            actor_role="REGULATOR",
            prediction_id=self.prediction_id,
            purpose="Regulatory compliance inspection"
        )

        self.assertTrue(result["authorized"])
        self.assertEqual(result["audit_event"]["status"], "SUCCESS")

        verification = self.integration.verify_custody(
            self.prediction_id
        )

        self.assertTrue(verification["valid"])

    def test_customer_cannot_replay_prediction(self):

        result = self.integration.replay_prediction(
            prediction_id=self.prediction_id,
            actor_id=self.customer_ref,
            actor_role="CUSTOMER",
            purpose="Customer replay attempt"
        )

        self.assertFalse(result["authorized"])
        self.assertEqual(result["audit_event"]["status"], "DENIED")

    def test_regulator_can_replay_prediction(self):

        result = self.integration.replay_prediction(
            prediction_id=self.prediction_id,
            actor_id="regulator-demo-01",
            actor_role="REGULATOR",
            purpose="Regulatory reproducibility verification"
        )

        self.assertTrue(result["authorized"])
        self.assertEqual(result["status"], "SUCCESS")

        self.assertIsNotNone(result["replay_result"])
        self.assertTrue(result["replay_result"]["replayed"])

        self.assertEqual(
            result["audit_event"]["action"],
            "REPLAY_EXECUTED"
        )

        self.assertEqual(
            result["audit_event"]["status"],
            "SUCCESS"
        )

        self.assertEqual(
            result["custody_event"]["event_type"],
            "PREDICTION_REPLAYED"
        )

        verification = self.integration.verify_custody(
            self.prediction_id
        )

        self.assertTrue(verification["valid"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
