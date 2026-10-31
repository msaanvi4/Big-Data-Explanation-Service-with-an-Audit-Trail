
"""
Person 3 Integration Adapter

Connects Person 2's DuckDB/Parquet repository and reproducibility
engine with Person 3's authorization, audit, custody and compliance
components.
"""

from dataclasses import asdict

from person2_query_interface.app.repository import get_repository
from person2_query_interface.app.reproducibility import ReproducibilityEngine

from ..audit.audit_logger import AuditLogger
from ..security.authorization import AuthorizationService
from ..custody.chain_of_custody import ChainOfCustody
from ..compliance.compliance_report import ComplianceReportGenerator


class Person3Integration:

    def __init__(
        self,
        repository=None,
        audit_logger=None,
        custody=None
    ):
        # Reuse Person 2's actual repository.
        self.repository = repository or get_repository()

        # Reuse Person 2's reproducibility engine.
        self.engine = ReproducibilityEngine(self.repository)

        # Initialize Person 3 components.
        self.audit_logger = audit_logger or AuditLogger()
        self.authorization = AuthorizationService(self.audit_logger)
        self.custody = custody or ChainOfCustody()

        # Connect the compliance report generator.
        self.compliance = ComplianceReportGenerator(
            repository=self.repository,
            reproducibility_engine=self.engine,
            audit_logger=self.audit_logger
        )

    def access_prediction(
        self,
        actor_id,
        actor_role,
        prediction_id,
        purpose
    ):
        """
        Authorize access to a prediction.

        Customers can access only their own synthetic request_trace_id.
        Regulators can access records for compliance purposes.
        Successful accesses are recorded in the custody ledger.
        """

        # Retrieve the actual prediction bundle.
        bundle = self.repository.get_decision_bundle(prediction_id)

        # Person 2 uses request_trace_id as synthetic customer reference.
        customer_ref = bundle.prediction.request_trace_id

        # Check authorization and log the access attempt.
        authorization_result = self.authorization.authorize(
            actor_id=actor_id,
            actor_role=actor_role,
            action="VIEW_DECISION",
            purpose=purpose,
            customer_ref=customer_ref,
            prediction_id=prediction_id
        )

        # Stop if access is denied.
        if not authorization_result["authorized"]:
            return {
                "authorized": False,
                "reason": authorization_result["reason"],
                "prediction_id": prediction_id,
                "audit_event": authorization_result["audit_event"]
            }

        # Hash references to stored artifacts.
        artifact_hash = self.custody.calculate_artifact_hash({
            "prediction_id": prediction_id,
            "input_hash": bundle.input.input_hash,
            "explanation_hash": bundle.explanation.explanation_hash,
            "model_version": bundle.model.model_version
        })

        # Record successful access in custody history.
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
            "audit_event": authorization_result["audit_event"],
            "custody_event": custody_event
        }

    def verify_custody(self, prediction_id):
        """Verify the hash chain for one prediction."""

        return self.custody.verify_prediction_chain(prediction_id)

    def get_custody_history(self, prediction_id):
        """Return custody events for one prediction."""

        return self.custody.get_prediction_history(prediction_id)

    def replay_prediction(
        self,
        prediction_id,
        actor_id,
        actor_role,
        purpose
    ):
        """
        Replay a prediction with authorization, audit logging,
        and chain-of-custody tracking.
        """

        # 1. Check replay authorization.
        authorization_result = self.authorization.authorize(
            actor_id=actor_id,
            actor_role=actor_role,
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

        # 3. Execute the replay.
        try:
            replay_result = self.engine.replay_prediction(prediction_id)

            replay_data = asdict(replay_result)

            # Distinguish a completed replay from a consistency mismatch.
            if replay_result.replayed:
                status = "SUCCESS"
                reason = (
                    "Replay completed and all consistency checks passed."
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
            # Record technical replay failure.
            status = "FAILED"
            reason = f"Replay execution failed: {type(exc).__name__}"

            replay_data = None
            error_type = type(exc).__name__

            artifact_hash = self.custody.calculate_artifact_hash({
                "prediction_id": prediction_id,
                "status": status,
                "error_type": error_type
            })

        # 4. Record replay in the audit trail.
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

        # 5. Record replay in chain of custody.
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

        # 6. Return the complete replay record.
        return {
            "authorized": True,
            "prediction_id": prediction_id,
            "status": status,
            "reason": reason,
            "replay_result": replay_data,
            "audit_event": replay_audit_event,
            "custody_event": custody_event
        }

    def generate_compliance_report(self):
        """Generate the integrated compliance report."""

        return self.compliance.generate_report()
