#!/usr/bin/env python3
"""
Boot-time governance lock reference for hermes-dashboard.service.
Emits exactly one audit/boot entry with required fields. No behavior change, no autonomy expansion.
"""
from pathlib import Path
import hashlib
import logging

_log = logging.getLogger(__name__)

def load_governance_lock():
    try:
        gov_path = Path("/home/chris/.hermes/docs/routing_governance.md")
        if gov_path.exists():
            data = gov_path.read_bytes()
            gov_hash = hashlib.sha256(data).hexdigest()
            event = f"event=routing_governance_loaded governance_path=/home/chris/.hermes/docs/routing_governance.md governance_version=1.0.0 governance_sha256={gov_hash} routing_config_path=/home/chris/.hermes/config.yaml routing_mode=deterministic status=loaded"
            print("BOOT:", event)
            _log.info(event)
            return event
        else:
            print("BOOT: governance_path missing")
            return "missing"
    except Exception as e:
        print("GOVERNANCE_BOOT_ERROR:", str(e))
        return "error"

if __name__ == "__main__":
    load_governance_lock()
else:
    # Run on import for boot visibility
    load_governance_lock()
