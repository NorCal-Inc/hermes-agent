#!/usr/bin/env python3
"""Production-roles registry integrity gate (rule 6, step 1 — approved 2026-10-07).

Pattern: ~/.hermes/security/skill-trust/verify.py. The registry next to this file is
pinned by a sha256 held HERE, in the verifier, not in the registry itself — a registry
that carried its own hash could re-pin itself. The verifier is the immutable root-owned
file; changing the registry requires Christopher to approve the new pin in this file.

Step 1 is report-only: the registry ships with zero profiles in every role and nothing
reads it to make an authorization decision yet. What this gate enforces today is that the
registry the boot payload reports is the exact registry Christopher approved.

Exit codes: 0 PASS; 1 hash drift or schema violation; 2 registry missing or unparseable.
Every outcome prints exactly one line starting with ``PRODUCTION ROLES:``.
"""
from pathlib import Path
import hashlib
import json
import sys

BASE = Path(__file__).resolve().parent
REG = BASE / "production-roles.json"

# sha256 of the approved production-roles.json bytes. Re-attest only on Christopher's approval.
PINNED_SHA256 = "44b16aa78ce90f8ea4693f8e321c69a2046f2f18eb92069ac1bc7f3a7b2faa4d"

ROLE_CLASSES = ("stripe_live", "deploy", "dns", "firewall", "ports", "service_units")
CHRISTOPHER_ONLY = frozenset({"stripe_live", "firewall", "ports", "service_units"})


def schema_errors(reg) -> list:
    errors = []
    if not isinstance(reg, dict):
        return ["registry root is not an object"]
    if not isinstance(reg.get("version"), int):
        errors.append("version missing or not an integer")
    roles = reg.get("roles")
    if not isinstance(roles, dict):
        return errors + ["roles missing or not an object"]
    expected = set(ROLE_CLASSES)
    got = set(roles)
    if got != expected:
        errors.append(f"role classes mismatch missing={sorted(expected - got)} extra={sorted(got - expected)}")
    for name in ROLE_CLASSES:
        role = roles.get(name)
        if not isinstance(role, dict):
            errors.append(f"role {name} is not an object")
            continue
        profiles = role.get("profiles")
        if not isinstance(profiles, list) or not all(isinstance(p, str) and p for p in profiles):
            errors.append(f"role {name}: profiles must be a list of non-empty strings")
        if not isinstance(role.get("christopher_only"), bool):
            errors.append(f"role {name}: christopher_only must be a boolean")
        elif (name in CHRISTOPHER_ONLY) != role["christopher_only"]:
            errors.append(f"role {name}: christopher_only must be {name in CHRISTOPHER_ONLY}")
        elif role["christopher_only"] and profiles:
            errors.append(f"role {name}: christopher_only role must have no profiles")
    return errors


def main() -> int:
    try:
        raw = REG.read_bytes()
    except FileNotFoundError:
        print(f"PRODUCTION ROLES: FAIL registry missing: {REG}")
        return 2
    except Exception as exc:
        print(f"PRODUCTION ROLES: FAIL registry unreadable: {exc}")
        return 2
    got = hashlib.sha256(raw).hexdigest()
    if got != PINNED_SHA256:
        print(f"PRODUCTION ROLES: FAIL registry drift expected={PINNED_SHA256[:16]} got={got[:16]} — re-attest only on Christopher's approval")
        return 1
    try:
        reg = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        print(f"PRODUCTION ROLES: FAIL registry unparseable: {exc}")
        return 2
    errors = schema_errors(reg)
    if errors:
        print("PRODUCTION ROLES: FAIL schema: " + "; ".join(errors))
        return 1
    roles = reg["roles"]
    assigned = sum(len(roles[n]["profiles"]) for n in ROLE_CLASSES)
    print(
        f"PRODUCTION ROLES: PASS version={reg['version']} mode={reg.get('mode', 'unspecified')} "
        f"roles={len(ROLE_CLASSES)} assigned_profiles={assigned} "
        f"christopher_only={','.join(n for n in ROLE_CLASSES if n in CHRISTOPHER_ONLY)} "
        f"sha256={got[:16]} policy=pinned-report-only-no-gate"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
