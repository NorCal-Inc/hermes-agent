import json
from pathlib import Path
import logging
from typing import Any

logger = logging.getLogger("routing_input_validator")

def validate_routing_input(payload: Any) -> dict:
    """Dedicated pre-classification validator. Only validates structure. Rejects malformed deterministically.
    No classification, routing, fallback, authority mutation, or auto-correction.
    """
    if payload is None:
        reason = "none_payload"
    elif not isinstance(payload, str) and not isinstance(payload, dict):
        reason = "unsupported_type"
    elif isinstance(payload, str):
        if not payload or payload.isspace():
            reason = "empty_or_whitespace_string"
        else:
            return {"status": "valid", "normalized": payload.strip()}
    elif isinstance(payload, dict):
        if not payload or "workload_content" not in payload and not payload.get("task"):
            reason = "missing_required_workload_content_field"
        else:
            return {"status": "valid", "normalized": payload}
    else:
        reason = "unsupported_payload_type"
    
    # Structured rejection (no implicit default, no silent coercion)
    rejection = {
        "event": "routing_input_rejected",
        "reason": reason,
        "payload_type": str(type(payload).__name__),
        "status": "rejected"
    }
    logger.info(json.dumps(rejection))
    print(json.dumps(rejection))  # for boot/audit visibility
    raise ValueError(f"Routing input rejected: {reason}")  # explicit exception for hard fail in tests/calls

# Minimal integration hook for routing_engine entry boundary (called before classify_workload)
def safe_classify_payload(payload: Any) -> str:
    """Thin wrapper for integration. Validates then passes normalized payload."""
    validated = validate_routing_input(payload)
    if validated["status"] == "valid":
        return validated["normalized"]
    raise ValueError("Invalid payload after validation")
