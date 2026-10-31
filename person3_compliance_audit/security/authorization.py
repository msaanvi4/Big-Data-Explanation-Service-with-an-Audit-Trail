
"""
Authorization Service

Checks whether customers, regulators and administrators
are allowed to access prediction and explanation records.

All valid-role access attempts are recorded in the audit trail.
"""

try:
    # Imports when used as part of the main project package
    from .access_matrix import is_action_allowed
    from ..audit.audit_logger import AuditLogger, VALID_ROLES

except ImportError:
    # Imports when tests are run from inside person3_compliance_audit
    from security.access_matrix import is_action_allowed
    from audit.audit_logger import AuditLogger, VALID_ROLES
class AuthorizationService:

    def __init__(self, audit_logger=None):
        """
        Initialize authorization service.

        An existing AuditLogger can be passed to share
        the same audit database.
        """

        self.audit_logger = audit_logger or AuditLogger()

    def authorize(
        self,
        actor_id,
        actor_role,
        action,
        purpose,
        customer_ref=None,
        prediction_id=None
    ):
        """
        Authorize an access request.

        Returns:
            {
                "authorized": True or False,
                "reason": "...",
                "audit_event": {...}
            }

        Customer ownership is checked by comparing
        actor_id with customer_ref.
        """

        # Validate identity
        if not actor_id or not str(actor_id).strip():
            raise ValueError("actor_id is required")

        if not actor_role:
            raise ValueError("actor_role is required")

        role = str(actor_role).upper()

        # The audit logger supports only these roles.
        # Reject unknown roles rather than recording them
        # under a misleading identity.
        if role not in VALID_ROLES:
            raise ValueError(f"Invalid actor role: {role}")

        if not action or not str(action).strip():
            raise ValueError("action is required")

        action = str(action).upper()

        # A missing purpose is a denied request, but it
        # must still be recorded in the audit trail.
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

        # ------------------------------------------
        # RULE 1: Purpose is mandatory
        # ------------------------------------------

        if purpose_missing:
            reason = "Access denied: purpose is required"

        # ------------------------------------------
        # RULE 2: Role-based action permission
        # ------------------------------------------

        elif not is_action_allowed(role, action):
            reason = (
                f"Access denied: {role} cannot perform "
                f"{action}"
            )

        # ------------------------------------------
        # RULE 3: Customer ownership validation
        # ------------------------------------------

        elif role == "CUSTOMER":

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

        # ------------------------------------------
        # RULE 4: Regulator access
        # ------------------------------------------

        elif role == "REGULATOR":

            authorized = True
            reason = (
                "Regulator access permitted for compliance purposes"
            )

        # ------------------------------------------
        # RULE 5: Administrator access
        # ------------------------------------------

        elif role == "ADMIN":

            authorized = True
            reason = "Administrator access permitted"

        # ------------------------------------------
        # RECORD ACCESS ATTEMPT
        # ------------------------------------------

        audit_event = self.audit_logger.log_event(
            actor_id=str(actor_id),
            actor_role=role,
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
            "audit_event": audit_event
        }
