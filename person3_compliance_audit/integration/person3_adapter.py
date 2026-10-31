
"""
Person 3 Integration Adapter

Connects Person 2's DuckDB/Parquet repository and
reproducibility engine with Person 3's:

1. Authentication
2. Authorization
3. Audit logging
4. Chain of custody
5. Compliance reporting

All prediction access and replay operations require
a verified authentication session.
"""

from dataclasses import asdict

from person2_query_interface.app.repository import get_repository
from person2_query_interface.app.reproducibility import ReproducibilityEngine

from ..audit.audit_logger import AuditLogger
from ..security.authentication import AuthenticationService
from ..security.authorization import AuthorizationService
from ..custody.chain_of_custody import ChainOfCustody
from ..compliance.compliance_report import ComplianceReportGenerator


class Person3Integration:

    def __init__(
        self,
        repository=None,
        audit_logger=None,
        custody=None,
        authentication_service=None
    ):

        # Person 2 repository.
        self.repository = repository or get_repository()

        # Person 2 reproducibility engine.
        self.engine = ReproducibilityEngine(self.repository)

        # Person 3 audit logger.
        self.audit_logger = audit_logger or AuditLogger()

        # Shared authentication service.
        self.authentication = (
            authentication_service or AuthenticationService()
        )

        # Authorization uses the same authentication service.
        self.authorization = AuthorizationService(
            audit_logger=self.audit_logger,
            authentication_service=self.authentication
        )

        # Chain of custody.
        self.custody = custody or ChainOfCustody()

        # Compliance report generator.
        self.compliance = ComplianceReportGenerator(
            repository=self.repository,
            reproducibility_engine=self.engine,
            audit_logger=self.audit_logger
        )

    # --------------------------------------------------
    # AUTHENTICATION HELPERS
    # --------------------------------------------------

    def bootstrap_admin(self, user_id, password):
        """
        Create the first administrator account.

        This is allowed only when the identity database
        contains no users.
        """

        return self.authentication.bootstrap_admin(
            user_id=user_id,
            password=password
        )

    def create_user(
        self,
        session_token,
        user_id,
        password,
        role
    ):
        """
        Create an account through an authenticated ADMIN.

        Only a verified administrator can create accounts.
        """

        return self.authentication.create_user(
            session_token=session_token,
            user_id=user_id,
            password=password,
            role=role
        )

    def login(self, user_id, password):
        """Authenticate a user and create a session."""

        return self.authentication.login(
            user_id=user_id,
            password=password
        )

    def logout(self, session_token):
        """Revoke an authenticated session."""

        return self.authentication.logout(session_token)

    # --------------------------------------------------
    # ACCESS PREDICTION
    # --------------------------------------------------

    def access_prediction(
        self,
        session_token,
        prediction_id,
        purpose
    ):
        """
        Access a prediction using an authenticated session.

        Customers can access only their own synthetic
        request_trace_id.

        Regulators can access records for compliance purposes.

        Successful accesses are recorded in chain of custody.
        """

        # 1. Validate authentication BEFORE repository access.
        identity = self.authentication.authenticate_session(
            session_token
        )

        if identity is None:

            return {
                "authorized": False,
                "reason": "Invalid or expired authentication session.",
                "prediction_id": prediction_id,
                "audit_event": None
            }

        # 2. Retrieve prediction bundle after authentication.
        bundle = self.repository.get_decision_bundle(
            prediction_id
        )

        # 3. Retrieve synthetic customer reference.
        customer_ref = bundle.prediction.request_trace_id

        # 4. Verify authorization and customer ownership.
        authorization_result = self.authorization.authorize(
            session_token=session_token,
            action="VIEW_DECISION",
            purpose=purpose,
            customer_ref=customer_ref,
            prediction_id=prediction_id
        )

        # 5. Stop if access is denied.
        if not authorization_result["authorized"]:

            return {
                "authorized": False,
                "reason": authorization_result["reason"],
                "prediction_id": prediction_id,
                "audit_event": authorization_result["audit_event"]
            }

        # Identity and role come from verified authorization.
        actor_id = authorization_result["actor_id"]
        actor_role = authorization_result["actor_role"]

        # 6. Calculate artifact hash.
        artifact_hash = self.custody.calculate_artifact_hash({
            "prediction_id": prediction_id,
            "input_hash": bundle.input.input_hash,
            "explanation_hash": bundle.explanation.explanation_hash,
            "model_version": bundle.model.model_version
        })

        # 7. Record successful access.
        custody_event = self.custody.record_event(
            prediction_id=prediction_id,
            event_type="PREDICTION_ACCESSED",
            actor_id=actor_id,
            actor_role=actor_role,
            artifact_type="PREDICTION_BUNDLE_REFERENCE",
            artifact_hash=artifact_hash,
            metadata={
                "action": "VIEW_DECISION",
                "purpose": purpose
            }
        )

        return {
            "authorized": True,
            "reason": authorization_result["reason"],
            "prediction_id": prediction_id,
            "actor_id": actor_id,
            "actor_role": actor_role,
            "audit_event": authorization_result["audit_event"],
            "custody_event": custody_event
        }

    # --------------------------------------------------
    # VERIFY CHAIN OF CUSTODY
    # --------------------------------------------------

    def verify_custody(self, prediction_id):
        """Verify the hash chain for one prediction."""

        return self.custody.verify_prediction_chain(
            prediction_id
        )

    # --------------------------------------------------
    # GET CUSTODY HISTORY
    # --------------------------------------------------

    def get_custody_history(self, prediction_id):
        """Return custody events for one prediction."""

        return self.custody.get_prediction_history(
            prediction_id
        )

    # --------------------------------------------------
    # REPLAY PREDICTION
    # --------------------------------------------------

    def replay_prediction(
        self,
        prediction_id,
        session_token,
        purpose
    ):
        """
        Replay a prediction using an authenticated session.

        Only roles permitted by the access matrix can replay.
        The actor identity and role come from the verified session.
        """

        # 1. Check authenticated replay authorization.
        authorization_result = self.authorization.authorize(
            session_token=session_token,
            action="REPLAY_PREDICTION",
            purpose=purpose,
            customer_ref=None,
            prediction_id=prediction_id
        )

        # 2. Stop if access is denied.
        if not authorization_result["authorized"]:

            return {
                "authorized": False,
                "prediction_id": prediction_id,
                "reason": authorization_result["reason"],
                "audit_event": authorization_result["audit_event"]
            }

        actor_id = authorization_result["actor_id"]
        actor_role = authorization_result["actor_role"]

        # 3. Execute replay.
        try:

            replay_result = self.engine.replay_prediction(
                prediction_id
            )

            replay_data = asdict(replay_result)

            if replay_result.replayed:

                status = "SUCCESS"

                reason = (
                    "Replay completed and all consistency "
                    "checks passed."
                )

            else:

                status = "SUCCESS"

                reason = (
                    "Replay completed, but one or more consistency "
                    "checks detected a mismatch."
                )

            artifact_hash = self.custody.calculate_artifact_hash(
                replay_data
            )

            error_type = None

        except Exception as exc:

            status = "FAILED"

            reason = (
                f"Replay execution failed: {type(exc).__name__}"
            )

            replay_data = None

            error_type = type(exc).__name__

            artifact_hash = self.custody.calculate_artifact_hash({
                "prediction_id": prediction_id,
                "status": status,
                "error_type": error_type
            })

        # 4. Record replay in audit trail.
        replay_audit_event = self.audit_logger.log_event(
            actor_id=actor_id,
            actor_role=actor_role,
            action="REPLAY_EXECUTED",
            purpose=purpose,
            customer_ref=None,
            prediction_id=prediction_id,
            status=status,
            reason=reason
        )

        # 5. Record replay in custody history.
        custody_event = self.custody.record_event(
            prediction_id=prediction_id,
            event_type="PREDICTION_REPLAYED",
            actor_id=actor_id,
            actor_role=actor_role,
            artifact_type="REPLAY_RESULT",
            artifact_hash=artifact_hash,
            metadata={
                "status": status,
                "replayed": (
                    replay_data["replayed"]
                    if replay_data is not None
                    else False
                ),
                "error_type": error_type,
                "purpose": purpose
            }
        )

        # 6. Return replay result.
        return {
            "authorized": True,
            "prediction_id": prediction_id,
            "actor_id": actor_id,
            "actor_role": actor_role,
            "status": status,
            "reason": reason,
            "replay_result": replay_data,
            "audit_event": replay_audit_event,
            "custody_event": custody_event
        }

    # --------------------------------------------------
    # COMPLIANCE REPORT
    # --------------------------------------------------

    def generate_compliance_report(self):
        """Generate integrated compliance report."""

        return self.compliance.generate_report()
