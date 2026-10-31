
import tempfile
import unittest

from pathlib import Path

from audit.audit_logger import AuditLogger
from security.authentication import AuthenticationService
from security.authorization import AuthorizationService


class TestAuthorization(unittest.TestCase):

    def setUp(self):

        self.temp_dir = tempfile.TemporaryDirectory()
        db_folder = Path(self.temp_dir.name)

        self.logger = AuditLogger(
            db_folder / "audit.db"
        )

        self.authentication = AuthenticationService(
            db_folder / "identity.db"
        )

        self.auth = AuthorizationService(
            audit_logger=self.logger,
            authentication_service=self.authentication
        )

        # --------------------------------------------------
        # CREATE FIRST ADMINISTRATOR
        # --------------------------------------------------

        self.authentication.bootstrap_admin(
            "admin_001",
            "AdministratorPassword@2026"
        )

        # Create administrator session.
        self.admin_login = self.authentication.login(
            "admin_001",
            "AdministratorPassword@2026"
        )

        self.admin_token = self.admin_login["session_token"]

        # --------------------------------------------------
        # CREATE CUSTOMER AND REGULATOR THROUGH ADMIN
        # --------------------------------------------------

        self.authentication.create_user(
            self.admin_token,
            "customer_001",
            "CustomerPassword@2026",
            "CUSTOMER"
        )

        self.authentication.create_user(
            self.admin_token,
            "regulator_001",
            "RegulatorPassword@2026",
            "REGULATOR"
        )

        # --------------------------------------------------
        # CREATE AUTHENTICATED SESSIONS
        # --------------------------------------------------

        self.customer_login = self.authentication.login(
            "customer_001",
            "CustomerPassword@2026"
        )

        self.regulator_login = self.authentication.login(
            "regulator_001",
            "RegulatorPassword@2026"
        )

        self.customer_token = self.customer_login["session_token"]
        self.regulator_token = self.regulator_login["session_token"]

    def tearDown(self):
        self.temp_dir.cleanup()

    # --------------------------------------------------
    # AUTHENTICATION AND ACCOUNT PROVISIONING TESTS
    # --------------------------------------------------

    def test_first_admin_can_be_created(self):

        with tempfile.TemporaryDirectory() as temp:

            auth_service = AuthenticationService(
                Path(temp) / "first_admin.db"
            )

            result = auth_service.bootstrap_admin(
                "first_admin",
                "FirstAdminPassword@2026"
            )

            self.assertTrue(result["registered"])
            self.assertEqual(result["role"], "ADMIN")

    def test_second_admin_bootstrap_is_rejected(self):

        with self.assertRaises(PermissionError):

            self.authentication.bootstrap_admin(
                "second_admin",
                "SecondAdminPassword@2026"
            )

    def test_admin_can_create_customer_account(self):

        result = self.authentication.create_user(
            self.admin_token,
            "customer_002",
            "CustomerTwoPassword@2026",
            "CUSTOMER"
        )

        self.assertTrue(result["registered"])
        self.assertEqual(result["role"], "CUSTOMER")

    def test_admin_can_create_regulator_account(self):

        result = self.authentication.create_user(
            self.admin_token,
            "regulator_002",
            "RegulatorTwoPassword@2026",
            "REGULATOR"
        )

        self.assertTrue(result["registered"])
        self.assertEqual(result["role"], "REGULATOR")

    def test_customer_cannot_create_accounts(self):

        with self.assertRaises(PermissionError):

            self.authentication.create_user(
                self.customer_token,
                "unauthorized_user",
                "UnauthorizedPassword@2026",
                "ADMIN"
            )

    def test_regulator_cannot_create_accounts(self):

        with self.assertRaises(PermissionError):

            self.authentication.create_user(
                self.regulator_token,
                "unauthorized_user",
                "UnauthorizedPassword@2026",
                "ADMIN"
            )

    def test_invalid_session_cannot_create_accounts(self):

        with self.assertRaises(PermissionError):

            self.authentication.create_user(
                "invalid-session-token",
                "unauthorized_user",
                "UnauthorizedPassword@2026",
                "ADMIN"
            )

    def test_admin_can_create_another_admin(self):

        result = self.authentication.create_user(
            self.admin_token,
            "admin_002",
            "SecondAdminPassword@2026",
            "ADMIN"
        )

        self.assertTrue(result["registered"])
        self.assertEqual(result["role"], "ADMIN")

    def test_invalid_role_is_rejected(self):

        with self.assertRaises(ValueError):

            self.authentication.create_user(
                self.admin_token,
                "invalid_role_user",
                "InvalidRolePassword@2026",
                "SUPERUSER"
            )

    # --------------------------------------------------
    # AUTHORIZATION TESTS
    # --------------------------------------------------

    def test_customer_can_access_own_record(self):

        result = self.auth.authorize(
            session_token=self.customer_token,
            action="VIEW_EXPLANATION",
            purpose="Understand my credit decision",
            customer_ref="customer_001",
            prediction_id="prediction_001"
        )

        self.assertTrue(result["authorized"])
        self.assertEqual(result["actor_role"], "CUSTOMER")

    def test_customer_cannot_access_another_record(self):

        result = self.auth.authorize(
            session_token=self.customer_token,
            action="VIEW_EXPLANATION",
            purpose="View another customer's decision",
            customer_ref="customer_002",
            prediction_id="prediction_002"
        )

        self.assertFalse(result["authorized"])
        self.assertEqual(
            result["audit_event"]["status"],
            "DENIED"
        )

    def test_regulator_can_access_records(self):

        result = self.auth.authorize(
            session_token=self.regulator_token,
            action="VIEW_DECISION",
            purpose="Regulatory investigation",
            customer_ref="customer_002",
            prediction_id="prediction_002"
        )

        self.assertTrue(result["authorized"])
        self.assertEqual(result["actor_role"], "REGULATOR")

    def test_customer_cannot_export_compliance_report(self):

        result = self.auth.authorize(
            session_token=self.customer_token,
            action="EXPORT_COMPLIANCE_REPORT",
            purpose="Download compliance report",
            customer_ref="customer_001"
        )

        self.assertFalse(result["authorized"])

    def test_customer_cannot_replay_prediction(self):

        result = self.auth.authorize(
            session_token=self.customer_token,
            action="REPLAY_PREDICTION",
            purpose="Attempt replay",
            prediction_id="prediction_001"
        )

        self.assertFalse(result["authorized"])

    def test_admin_can_perform_administrative_action(self):

        result = self.auth.authorize(
            session_token=self.admin_token,
            action="DELETE_RECORD",
            purpose="Approved administrative operation",
            customer_ref="customer_001"
        )

        self.assertTrue(result["authorized"])
        self.assertEqual(result["actor_role"], "ADMIN")

    def test_invalid_session_is_rejected(self):

        result = self.auth.authorize(
            session_token="invalid-session-token",
            action="VIEW_DECISION",
            purpose="Invalid session test",
            customer_ref="customer_001"
        )

        self.assertFalse(result["authorized"])
        self.assertIsNone(result["audit_event"])

    def test_logged_out_session_is_rejected(self):

        self.authentication.logout(self.customer_token)

        result = self.auth.authorize(
            session_token=self.customer_token,
            action="VIEW_DECISION",
            purpose="Attempt after logout",
            customer_ref="customer_001"
        )

        self.assertFalse(result["authorized"])

    def test_missing_purpose_is_denied_and_logged(self):

        result = self.auth.authorize(
            session_token=self.customer_token,
            action="VIEW_DECISION",
            purpose="",
            customer_ref="customer_001"
        )

        self.assertFalse(result["authorized"])
        self.assertEqual(
            result["audit_event"]["status"],
            "DENIED"
        )

    def test_authorization_events_preserve_hash_chain(self):

        self.auth.authorize(
            session_token=self.customer_token,
            action="VIEW_DECISION",
            purpose="View own decision",
            customer_ref="customer_001"
        )

        self.auth.authorize(
            session_token=self.customer_token,
            action="VIEW_DECISION",
            purpose="Attempt another customer's decision",
            customer_ref="customer_002"
        )

        result = self.logger.verify_chain()

        self.assertTrue(result["valid"])
        self.assertEqual(result["checked_records"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
