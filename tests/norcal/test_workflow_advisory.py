"""Workflow findings stay advisory while VPS security failures remain blocking."""
import json
import re
import time

import pytest

from tests.norcal.test_boot_context_kanban_pin import _BootCopy


def _boot(tmp_path):
    boot = _BootCopy(tmp_path)
    doctrine = boot.root / ".hermes/doctrine"
    doctrine.mkdir(parents=True, exist_ok=True)
    for name in ("hierarchy.md", "sovereignty.md", "data-isolation.md"):
        (doctrine / name).write_text("Security boundary fixture\n")
    return boot


def _state(payload, name):
    block = payload.split("<shared-boot-state>", 1)[1].split("</shared-boot-state>", 1)[0]
    return re.search(r"(?m)^" + re.escape(name) + r": (.+)$", block).group(1)


@pytest.mark.parametrize("health_status", ["DEGRADED", "ESCALATED", None])
def test_health_and_missing_continuity_do_not_block_work(tmp_path, health_status):
    boot = _boot(tmp_path)
    if health_status is not None:
        heartbeat = boot.root / ".hermes/state/system-health-controller/heartbeat-light.json"
        heartbeat.parent.mkdir(parents=True)
        heartbeat.write_text(json.dumps({"status": health_status, "finished_ts": time.time()}))
    payload = boot.run(pin=None)
    assert _state(payload, "CONTINUITY") == "FAIL"
    assert _state(payload, "OPERATIONAL").startswith("WARN")
    assert _state(payload, "BOOT STATUS") == "COMPLETE"
    assert _state(payload, "READY") == "YES"
    assert "Continuity advisory (non-blocking)" in payload


@pytest.mark.parametrize("security_gate", ["skill-trust", "production-roles"])
def test_vps_security_failure_still_blocks_work(tmp_path, security_gate):
    boot = _boot(tmp_path)
    verifier = boot.root / ".hermes/security" / security_gate / "verify.py"
    verifier.write_text("#!/usr/bin/env python3\nprint('injected security failure')\nraise SystemExit(2)\n")
    payload = boot.run(pin=None)
    assert _state(payload, "TECHNICAL BOOT") == "FAIL"
    assert _state(payload, "READY") == "NO"
    assert "injected security failure" in payload
