import json
import hashlib
import sys
from pathlib import Path
import logging
import yaml

logger = logging.getLogger("config_validator")

def validate_routing_config():
    """Lightweight config integrity validator. Separate from routing_engine. No behavior change."""
    config_path = Path("/home/chris/.hermes/config.yaml")
    if not config_path.exists():
        payload = {"event": "routing_config_validation_failed", "status": "config_missing"}
        logger.error(json.dumps(payload))
        print(json.dumps(payload))
        sys.exit(1)
    
    with open(config_path) as f:
        config = yaml.safe_load(f)
    
    routing = config.get("routing", {})
    if not routing:
        payload = {"event": "routing_config_validation_failed", "status": "missing_routing_section"}
        logger.error(json.dumps(payload))
        print(json.dumps(payload))
        sys.exit(1)
    
    schema_version = routing.get("schema_version")
    if not schema_version or schema_version != "1.0":
        payload = {"event": "routing_config_validation_failed", "status": "schema_version_mismatch", "expected": "1.0", "actual": schema_version}
        logger.error(json.dumps(payload))
        print(json.dumps(payload))
        sys.exit(1)
    
    hierarchy = routing.get("hierarchy", {})
    classification = routing.get("classification", {})
    fallback = routing.get("fallback", {})
    mode = routing.get("routing_mode", routing.get("mode", ""))
    
    if mode != "deterministic":
        payload = {"event": "routing_config_validation_failed", "status": "missing_deterministic_mode"}
        logger.error(json.dumps(payload))
        print(json.dumps(payload))
        sys.exit(1)
    
    # Unique workload classes
    classes = set(hierarchy.keys())
    if len(classes) != len(hierarchy):
        payload = {"event": "routing_config_validation_failed", "status": "duplicate_workload_ownership"}
        logger.error(json.dumps(payload))
        print(json.dumps(payload))
        sys.exit(1)
    
    # Complete provider mapping
    required = {"orchestration", "operational", "engineering", "research"}
    if not required.issubset(hierarchy.keys()):
        payload = {"event": "routing_config_validation_failed", "status": "incomplete_provider_mapping"}
        logger.error(json.dumps(payload))
        print(json.dumps(payload))
        sys.exit(1)
    
    # No unknown providers in hierarchy
    known_providers = {"openai", "xai", "deepseek"}
    for entry in hierarchy.values():
        if entry.get("provider") not in known_providers:
            payload = {"event": "routing_config_validation_failed", "status": "unknown_provider"}
            logger.error(json.dumps(payload))
            print(json.dumps(payload))
            sys.exit(1)
    
    # No forbidden fallback paths
    forbidden = {"deepseek": "orchestration"}
    for k, v in fallback.items():
        if "deepseek" in str(v).lower() and "orchestration" in str(v).lower():
            payload = {"event": "routing_config_validation_failed", "status": "forbidden_fallback_path"}
            logger.error(json.dumps(payload))
            print(json.dumps(payload))
            sys.exit(1)
    
    # Compute SHA
    data = config_path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    
    providers = list(known_providers)
    payload = {
        "event": "routing_config_validated",
        "routing_config_path": str(config_path),
        "routing_config_sha256": sha,
        "routing_schema_version": schema_version,
        "routing_mode": "deterministic",
        "providers_loaded": providers,
        "status": "validated"
    }
    logger.info(json.dumps(payload))
    print(json.dumps(payload))
    return payload

if __name__ == "__main__":
    validate_routing_config()
else:
    # Run on import for startup
    validate_routing_config()
