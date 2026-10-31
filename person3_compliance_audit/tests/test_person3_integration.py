
"""
Person 3 Integration Tests

Tests:
1. Person 2 DuckDB/Parquet repository
2. Authentication and session verification
3. Role-based authorization
4. Audit logging
5. Chain of custody
6. Prediction reproducibility
7. Administrator-controlled account provisioning
"""

import unittest
import tempfile

from pathlib import Path

from person2_query_interface.app.repository import get_repository

from person3_compliance_audit.audit.audit_logger import AuditLogger
from person3_compliance_audit.security.authentication import (
    AuthenticationService
)
from person3_compliance_audit.custody.chain_of_custody import ChainOfCustody
from person3_compliance_audit.integration.person3_adapter import (
    Person3Integration
)


class TestPerson3Integration(unittest.TestCase):

    def setUp(self):

        self.temp_dir = tempfile.TemporaryDirectory()
        db_folder = Path(self.temp_dir.name)

        # Connect to Person 2's actual repository.
        self.repository = get_repository()

        self.prediction_id = (
            self.repository.list_prediction_ids()[0]
        )

        self.bundle = self.repository.get_decision_bundle(
            self.prediction_id
        )

        self.customer_ref = (
            self.bundle.prediction.request_trace_id
        )

        # Create isolated databases.
        self.audit_logger = AuditLogger(
            db_path=str(db_folder / "audit.db")
        )

        self.custody = ChainOfCustody(
            db_path=str(db_folder / "custody.db")
        )

        self.authentication = AuthenticationService(
            db_path=str(db_folder / "identity.db")
        )

        # --------------------------------------------------
        # STEP 1: CREATE FIRST ADMINISTRATOR
        # --------------------------------------------------

        self.authentication.bootstrap_admin(
            "admin_integration_001",
            "AdminIntegrationPassword@2026"
        )

        # --------------------------------------------------
        # STEP 2: ADMINISTRATOR LOGIN
        # --------------------------------------------------

        self.admin_login = self.authentication.login(
            "admin_integration_001",
            "AdminIntegrationPassword@2026"
        )

        self.admin_token = (
            self.admin_login["session_token"]
        )

        # --------------------------------------------------
        # STEP 3: CREATE CUSTOMER ACCOUNTS THROUGH ADMIN
        # --------------------------------------------------

        self.authentication.create_user(
            self.admin_token,
            self.customer_ref,
            "CustomerPassword@2026",
            "CUSTOMER"
        )

        self.authentication.create_user(
            self.admin_token,
            "different-synthetic-customer",
            "DifferentPassword@2026",
            "CUSTOMER"
        )

        # --------------------------------------------------
        # STEP 4: CREATE REGULATOR THROUGH ADMIN
        # --------------------------------------------------

        self.authentication.create_user(
            self.admin_token,
            "regulator-demo-01",
            "RegulatorPassword@2026",
            "REGULATOR"
        )

        # --------------------------------------------------
        # STEP 5: CREATE AUTHENTICATED USER SESSIONS
        # --------------------------------------------------

        self.customer_login = self.authentication.login(
            self.customer_ref,
            "CustomerPassword@2026"
        )

        self.other_customer_login = self.authentication.login(
            "different-synthetic-customer",
            "DifferentPassword@2026"
        )

        self.regulator_login = self.authentication.login(
            "regulator-demo-01",
            "RegulatorPassword@2026"
        )

        self.customer_token = (
            self.customer_login["session_token"]
        )

        self.other_customer_token = (
            self.other_customer_login["session_token"]
        )

        self.regulator_token = (
            self.regulator_login["session_token"]
        )

        # --------------------------------------------------
        # STEP 6: INITIALIZE INTEGRATED SYSTEM
        # --------------------------------------------------

        self.integration = Person3Integration(
            repository=self.repository,
            audit_logger=self.audit_logger,
            custody=self.custody,
            authentication_service=self.authentication
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    # --------------------------------------------------
    # TEST 1: CUSTOMER CAN ACCESS OWN PREDICTION
    # --------------------------------------------------

    def test_customer_can_access_own_prediction(self):

        result = self.integration.access_prediction(
            session_token=self.customer_token,
            prediction_id=self.prediction_id,
            purpose="Customer decision review"
        )

        self.assertTrue(result["authorized"])

        self.assertEqual(
            result["actor_role"],
            "CUSTOMER"
        )

        self.assertEqual(
            result["audit_event"]["status"],
            "SUCCESS"
        )

        self.assertEqual(
            result["custody_event"]["event_type"],
            "PREDICTION_ACCESSED"
        )

        verification = self.integration.verify_custody(
            self.prediction_id
        )

        self.assertTrue(verification["valid"])

    # --------------------------------------------------
    # TEST 2: CUSTOMER CANNOT ACCESS ANOTHER PREDICTION
    # --------------------------------------------------

    def test_customer_cannot_access_another_customer_prediction(self):

        result = self.integration.access_prediction(
            session_token=self.other_customer_token,
            prediction_id=self.prediction_id,
            purpose="Unauthorized access attempt"
        )

        self.assertFalse(result["authorized"])

        self.assertEqual(
            result["audit_event"]["status"],
            "DENIED"
        )

    # --------------------------------------------------
    # TEST 3: REGULATOR CAN ACCESS PREDICTION
    # --------------------------------------------------

    def test_regulator_can_access_prediction(self):

        result = self.integration.access_prediction(
            session_token=self.regulator_token,
            prediction_id=self.prediction_id,
            purpose="Regulatory compliance inspection"
        )

        self.assertTrue(result["authorized"])

        self.assertEqual(
            result["actor_role"],
            "REGULATOR"
        )

        self.assertEqual(
            result["audit_event"]["status"],
            "SUCCESS"
        )

        verification = self.integration.verify_custody(
            self.prediction_id
        )

        self.assertTrue(verification["valid"])

    # --------------------------------------------------
    # TEST 4: CUSTOMER CANNOT REPLAY PREDICTION
    # --------------------------------------------------

    def test_customer_cannot_replay_prediction(self):

        result = self.integration.replay_prediction(
            prediction_id=self.prediction_id,
            session_token=self.customer_token,
            purpose="Customer replay attempt"
        )

        self.assertFalse(result["authorized"])

        self.assertEqual(
            result["audit_event"]["status"],
            "DENIED"
        )

    # --------------------------------------------------
    # TEST 5: REGULATOR CAN REPLAY PREDICTION
    # --------------------------------------------------

    def test_regulator_can_replay_prediction(self):

        result = self.integration.replay_prediction(
            prediction_id=self.prediction_id,
            session_token=self.regulator_token,
            purpose="Regulatory reproducibility verification"
        )

        self.assertTrue(result["authorized"])

        self.assertEqual(
            result["actor_role"],
            "REGULATOR"
        )

        self.assertEqual(
            result["status"],
            "SUCCESS"
        )

        self.assertIsNotNone(
            result["replay_result"]
        )

        self.assertTrue(
            result["replay_result"]["replayed"]
        )

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

    # --------------------------------------------------
    # TEST 6: INVALID SESSION CANNOT ACCESS PREDICTION
    # --------------------------------------------------

    def test_invalid_session_cannot_access_prediction(self):

        result = self.integration.access_prediction(
            session_token="invalid-session",
            prediction_id=self.prediction_id,
            purpose="Invalid session attempt"
        )

        self.assertFalse(result["authorized"])

        self.assertIsNone(
            result["audit_event"]
        )

    # --------------------------------------------------
    # TEST 7: LOGGED-OUT CUSTOMER CANNOT ACCESS PREDICTION
    # --------------------------------------------------

    def test_logged_out_customer_cannot_access_prediction(self):

        self.integration.logout(self.customer_token)

        result = self.integration.access_prediction(
            session_token=self.customer_token,
            prediction_id=self.prediction_id,
            purpose="Access after logout"
        )

        self.assertFalse(result["authorized"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
