
"""
Authorization Service

Enforces role-based access control using verified
identities obtained from AuthenticationService.

Callers cannot choose their own role.
"""

try:
    from .access_matrix import is_action_allowed
    from .authentication import AuthenticationService
    from ..audit.audit_logger import AuditLogger

except ImportError:
    from security.access_matrix import is_action_allowed
    from security.authentication import AuthenticationService
    from audit.audit_logger import AuditLogger


class AuthorizationService:

    def __init__(
        self,
        audit_logger=None,
        authentication_service=None
    ):

        self.audit_logger = audit_logger or AuditLogger()

        self.authentication = (
            authentication_service or AuthenticationService()
        )

    def authorize(
        self,
        session_token,
        action,
        purpose,
        customer_ref=None,
        prediction_id=None
    ):
        """
        Verify the session first, then authorize the action.

        Identity and role are retrieved from the trusted
        authentication database.
        """

        if not session_token:
            return {
                "authorized": False,
                "reason": "Access denied: authentication required",
                "audit_event": None
            }

        # Step 1: Verify the session.
        identity = self.authentication.authenticate_session(
            session_token
        )

        if identity is None:
            return {
                "authorized": False,
                "reason": "Access denied: invalid or expired session",
                "audit_event": None
            }

        # Step 2: Obtain verified identity.
        actor_id = identity["user_id"]
        actor_role = identity["role"]

        # Step 3: Validate the requested action.
        if not action or not str(action).strip():
            raise ValueError("Action is required")

        action = str(action).upper()

        purpose_missing = (
            not purpose or not str(purpose).strip()
        )

        audit_purpose = (
            str(purpose).strip()
            if not purpose_missing
            else "Unspecified purpose"
        )

        authorized = False
        reason = ""

        if purpose_missing:

            reason = "Access denied: purpose is required"

        elif not is_action_allowed(actor_role, action):

            reason = (
                f"Access denied: {actor_role} cannot perform {action}"
            )

        elif actor_role == "CUSTOMER":

            if not customer_ref:

                reason = (
                    "Access denied: customer reference is required"
                )

            elif str(actor_id) != str(customer_ref):

                reason = (
                    "Access denied: customers can access "
                    "only their own records"
                )

            else:

                authorized = True
                reason = "Customer ownership verified"

        elif actor_role == "REGULATOR":

            authorized = True
            reason = (
                "Regulator access permitted for compliance purposes"
            )

        elif actor_role == "ADMIN":

            authorized = True
            reason = "Administrator access permitted"

        # Step 4: Record the authorization decision.
        audit_event = self.audit_logger.log_event(
            actor_id=actor_id,
            actor_role=actor_role,
            customer_ref=customer_ref,
            prediction_id=prediction_id,
            action=action,
            purpose=audit_purpose,
            status="SUCCESS" if authorized else "DENIED",
            reason=reason
        )

        return {
            "authorized": authorized,
            "reason": reason,
            "actor_id": actor_id,
            "actor_role": actor_role,
            "audit_event": audit_event
        }
