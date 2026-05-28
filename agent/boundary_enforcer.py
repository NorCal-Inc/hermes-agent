import json
import logging
from typing import Dict, List, Any

logger = logging.getLogger("boundary_enforcer")

class BoundaryEnforcer:
    """Hard operational boundary separation. Backend/runtime enforcement only.
    No UI reliance. Deterministic capability matrix. Structured rejection events.
    No autonomy, no dynamic maps, no routing/recovery/provider changes.
    """

    CAPABILITY_MATRIX: Dict[str, List[str]] = {
        "erika": ["orchestration", "governance_reasoning", "escalation", "topology", "approval", "planning", "risk_evaluation"],
        "team_leader": ["coordination", "assign"],
        "worker": ["execution"],
        "dashboard": ["observational"]
    }

    def __init__(self):
        self.matrix = self.CAPABILITY_MATRIX

    def enforce(self, role: str, action: str, context: Dict = None) -> Dict[str, Any]:
        """Deterministic capability validator. Hard-fail on unauthorized escalation."""
        allowed = self.matrix.get(role.lower(), [])
        if action not in allowed:
            rejection = {
                "event": "authority_boundary_rejected",
                "actor_role": role,
                "requested_action": action,
                "reason": f"{action} not in allowed for {role}",
                "status": "rejected"
            }
            logger.info(json.dumps(rejection))
            print(json.dumps(rejection))
            raise PermissionError(f"Authority boundary rejected: {action} for {role}")
        return {"status": "allowed", "role": role, "action": action}

    def is_observational_only(self, role: str = "dashboard") -> bool:
        """Dashboard remains observational-only at backend layer."""
        return "observational" in self.matrix.get(role.lower(), []) and len(self.matrix.get(role.lower(), [])) == 1

# Singleton
boundary_enforcer = BoundaryEnforcer()

def enforce_boundary(role: str, action: str, context: Dict = None) -> Dict[str, Any]:
    """Public minimal integration hook."""
    return boundary_enforcer.enforce(role, action, context)
