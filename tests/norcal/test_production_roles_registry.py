"""Production-roles registry gate (rule 6 step 1, approved 2026-10-07): pinned + fail closed.

Two things are under test.

1. ``norcal/security/production-roles/verify.py`` pins the registry next to it by a sha256
   held in the verifier. The shipped registry must pass as-is with zero assigned profiles;
   any edit, a missing file, or a shape violation must produce a non-zero exit and a single
   ``PRODUCTION ROLES: FAIL`` line.

2. ``norcal/boot/hermes-shared-boot-context`` runs the *installed* verifier right after the
   skill-trust gate and echoes its line into the payload. A failing or absent verifier must
   become a BLOCKING FAILURE, not a warning. The boot generator roots every path under one
   ``CH`` constant, so — following ``tests/norcal/conftest.py`` — the test copies it into
   ``tmp_path``, repoints ``CH`` there, and plants stub verifiers. The real boot script is
   never run against live state here.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ROLES_SRC = REPO / "norcal" / "security" / "production-roles"
BOOT_SRC = REPO / "norcal" / "boot"
ROLE_CLASSES = ("stripe_live", "deploy", "dns", "firewall", "ports", "service_units")
# 2026-10-09 Christopher: every role is Christopher-authorized only; executed by Claude Code or Codex.
CHRISTOPHER_ONLY = set(ROLE_CLASSES)
AUTHORIZED_EXECUTORS = {"claude", "codex"}


def _run_verifier(base: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(base / "verify.py")],
        capture_output=True, text=True, timeout=30,
    )


def _staged_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "production-roles"
    shutil.copytree(ROLES_SRC, dst)
    return dst


def _single_status_line(out: str) -> str:
    lines = [ln for ln in out.splitlines() if ln.startswith("PRODUCTION ROLES:")]
    assert len(lines) == 1, out
    return lines[0]


# ---------------------------------------------------------------------------
# Registry contents: the approved step-1 shape, empty of profiles.
# ---------------------------------------------------------------------------

def test_shipped_registry_is_empty_and_marks_locked_surfaces_christopher_only():
    reg = json.loads((ROLES_SRC / "production-roles.json").read_text(encoding="utf-8"))
    assert isinstance(reg["version"], int)
    assert set(reg["roles"]) == set(ROLE_CLASSES)
    for name, role in reg["roles"].items():
        assert role["profiles"] == [], f"{name} ships with profiles assigned"
        assert role["christopher_only"] is (name in CHRISTOPHER_ONLY), name
    assert set(reg["christopher_authorized_executors"]) == AUTHORIZED_EXECUTORS


# ---------------------------------------------------------------------------
# Verifier: intact passes; edited, missing, malformed fail closed.
# ---------------------------------------------------------------------------

def test_intact_registry_passes():
    p = _run_verifier(ROLES_SRC)
    line = _single_status_line(p.stdout)
    assert p.returncode == 0, p.stdout + p.stderr
    assert line.startswith("PRODUCTION ROLES: PASS")
    assert "assigned_profiles=0" in line


def test_edited_registry_is_caught(tmp_path):
    base = _staged_copy(tmp_path)
    reg_path = base / "production-roles.json"
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    reg["roles"]["deploy"]["profiles"].append("sneaky_profile")
    reg_path.write_text(json.dumps(reg, indent=2) + "\n", encoding="utf-8")

    p = _run_verifier(base)
    assert p.returncode != 0
    line = _single_status_line(p.stdout)
    assert line.startswith("PRODUCTION ROLES: FAIL")
    assert "drift" in line


def test_whitespace_only_edit_is_still_drift(tmp_path):
    """The pin is over bytes: a re-serialised but semantically identical file is not approved."""
    base = _staged_copy(tmp_path)
    reg_path = base / "production-roles.json"
    reg_path.write_text(reg_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    p = _run_verifier(base)
    assert p.returncode != 0
    assert "drift" in _single_status_line(p.stdout)


def test_missing_registry_is_caught(tmp_path):
    base = _staged_copy(tmp_path)
    (base / "production-roles.json").unlink()
    p = _run_verifier(base)
    assert p.returncode != 0
    line = _single_status_line(p.stdout)
    assert line.startswith("PRODUCTION ROLES: FAIL")
    assert "missing" in line


def test_unparseable_registry_is_caught(tmp_path):
    base = _staged_copy(tmp_path)
    (base / "production-roles.json").write_text("{not json", encoding="utf-8")
    p = _run_verifier(base)
    assert p.returncode != 0
    assert _single_status_line(p.stdout).startswith("PRODUCTION ROLES: FAIL")


def _schema_errors():
    spec = importlib.util.spec_from_file_location("production_roles_verify_under_test", ROLES_SRC / "verify.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.schema_errors


@pytest.mark.parametrize("mutate, needle", [
    (lambda r: r["roles"]["firewall"]["profiles"].append("erika"), "christopher_only role must have no profiles"),
    (lambda r: r["roles"]["ports"].__setitem__("christopher_only", False), "christopher_only must be True"),
    (lambda r: r["roles"].pop("stripe_live"), "role classes mismatch"),
    (lambda r: r["roles"].__setitem__("database", {"christopher_only": False, "profiles": []}), "role classes mismatch"),
    (lambda r: r["roles"]["dns"].__setitem__("profiles", "all"), "profiles must be a list"),
    (lambda r: r.__setitem__("version", "1"), "version missing or not an integer"),
    (lambda r: r["roles"]["deploy"]["profiles"].append("orion_formation_services_lead"), "christopher_only role must have no profiles"),
    (lambda r: r["christopher_authorized_executors"].append("default"), "christopher_authorized_executors must be"),
    (lambda r: r.pop("christopher_authorized_executors"), "christopher_authorized_executors must be"),
])
def test_schema_rejects_shape_violations_even_if_repinned(mutate, needle):
    """A future re-pin cannot smuggle in a profile on a Christopher-only role or a new role class."""
    schema_errors = _schema_errors()
    reg = json.loads((ROLES_SRC / "production-roles.json").read_text(encoding="utf-8"))
    assert schema_errors(reg) == []
    mutate(reg)
    errors = schema_errors(reg)
    assert any(needle in e for e in errors), errors


# ---------------------------------------------------------------------------
# Boot wiring: the line is emitted and a failing/absent verifier blocks the boot.
# ---------------------------------------------------------------------------

class _BootCopy:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "ch"
        self.boot = self.root / "boot"
        self.boot.mkdir(parents=True)
        shutil.copy2(BOOT_SRC / "boot-constraints.md", self.boot / "boot-constraints.md")
        src = (BOOT_SRC / "hermes-shared-boot-context").read_text(encoding="utf-8")
        new, count = re.subn(r"(?m)^CH = Path\([^)]*\)", f"CH = Path({str(self.root)!r})", src)
        assert count == 1, f"expected exactly one CH assignment to rewrite, found {count}"
        self.script = self.boot / "hermes-shared-boot-context"
        self.script.write_text(new, encoding="utf-8")
        # The skill-trust gate is wired identically; keep it green so the only signal under
        # test is the production-roles gate.
        self.plant("skill-trust", 'print("SKILL TRUST: PASS stub")')

    def plant(self, gate: str, body: str) -> Path:
        d = self.root / ".hermes" / "security" / gate
        d.mkdir(parents=True, exist_ok=True)
        p = d / "verify.py"
        p.write_text("#!/usr/bin/env python3\nimport sys\n" + body + "\n", encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return p

    def run(self) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN")}
        env.pop("HERMES_HOME", None)
        p = subprocess.run(
            [sys.executable, str(self.script)],
            capture_output=True, text=True, timeout=120, cwd=str(self.root), env=env,
        )
        assert "<shared-boot-state>" in p.stdout, p.stderr
        return p.stdout


def _blocking_failures(payload: str) -> list[str]:
    block = payload.split("BLOCKING FAILURES:", 1)[1].split("NON-BLOCKING WARNINGS:", 1)[0]
    return [ln[2:] for ln in block.splitlines() if ln.startswith("- ")]


def test_boot_payload_carries_the_production_roles_line(tmp_path):
    boot = _BootCopy(tmp_path)
    boot.plant("production-roles", 'print("PRODUCTION ROLES: PASS stub assigned_profiles=0")')
    payload = boot.run()
    assert re.search(r"(?m)^PRODUCTION ROLES: PASS stub assigned_profiles=0$", payload), payload
    assert "PRODUCTION ROLES REGISTRY" in payload
    assert not any("production-roles" in f for f in _blocking_failures(payload))


@pytest.mark.parametrize("rc", [1, 2])
def test_failing_verifier_is_a_boot_blocker(tmp_path, rc):
    boot = _BootCopy(tmp_path)
    boot.plant("production-roles", f'print("PRODUCTION ROLES: FAIL registry drift"); sys.exit({rc})')
    payload = boot.run()
    failures = _blocking_failures(payload)
    assert any(f.startswith(f"production-roles registry gate failed rc={rc}") for f in failures), failures
    assert re.search(r"(?m)^BOOT STATUS: DEGRADED", payload)


def test_absent_verifier_is_a_boot_blocker(tmp_path):
    """Fail closed: no verifier installed is indistinguishable from a failed verification."""
    boot = _BootCopy(tmp_path)
    payload = boot.run()
    failures = _blocking_failures(payload)
    assert any(f.startswith("production-roles registry gate failed rc=") for f in failures), failures
    assert re.search(r"(?m)^PRODUCTION ROLES: FAIL rc=\d+ — ", payload), payload


def test_silent_verifier_is_a_boot_blocker_with_a_named_line(tmp_path):
    boot = _BootCopy(tmp_path)
    boot.plant("production-roles", "sys.exit(1)")
    payload = boot.run()
    assert any("production-roles registry gate failed rc=1" in f for f in _blocking_failures(payload))
    assert re.search(r"(?m)^PRODUCTION ROLES: FAIL rc=1 — verifier returned no output$", payload), payload
