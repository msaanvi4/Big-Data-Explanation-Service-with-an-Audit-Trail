
"""
Role-Based Access Control Matrix

Defines which actions each role can perform.
"""

ACCESS_MATRIX = {
    "CUSTOMER": {
        "VIEW_DECISION",
        "VIEW_EXPLANATION",
        "VIEW_OWN_AUDIT"
    },

    "REGULATOR": {
        "VIEW_DECISION",
        "VIEW_EXPLANATION",
        "VIEW_AUDIT",
        "EXPORT_COMPLIANCE_REPORT",
        "VERIFY_AUDIT_TRAIL",
        "REPLAY_PREDICTION"
    },

    "ADMIN": {
        "*"
    }
}


def is_action_allowed(actor_role, action):
    """
    Check whether a role is permitted to perform an action.
    """

    role = actor_role.upper()
    action = action.upper()

    allowed_actions = ACCESS_MATRIX.get(role, set())

    return (
        "*" in allowed_actions
        or action in allowed_actions
    )
