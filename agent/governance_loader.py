import json
from pathlib import Path
import hashlib
import logging

logger = logging.getLogger("governance")

def get_governance_metadata():
    """Minimal helper: read governance doc, compute SHA256, expose metadata payload as structured JSON.
    No routing, authority, fallback, provider logic. Only boot visibility.
    """
    gov_path = Path("/home/chris/.hermes/docs/routing_governance.md")
    if not gov_path.exists():
        payload = {"event": "routing_governance_loaded", "status": "missing"}
        logger.info(json.dumps(payload))
        print(json.dumps(payload))
        return payload
    data = gov_path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    payload = {
        "event": "routing_governance_loaded",
        "governance_path": str(gov_path),
        "governance_version": "1.0.0",
        "governance_sha256": sha,
        "routing_config_path": "/home/chris/.hermes/config.yaml",
        "routing_mode": "deterministic",
        "status": "loaded"
    }
    logger.info(json.dumps(payload))
    print(json.dumps(payload))
    return payload

if __name__ == "__main__":
    get_governance_metadata()
else:
    # Run on import for boot
    get_governance_metadata()
