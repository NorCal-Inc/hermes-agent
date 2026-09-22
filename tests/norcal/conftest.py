"""Harness for driving the real North Caledonia boot gate scripts in isolation.

``norcal/boot/claude-session-start-gate.py`` and ``claude-tool-guard.py`` are hook
scripts, not importable modules: they run their logic at import time, resolve the boot
wrapper relative to ``__file__``, and read/write a fixed session-state directory under
``/home/chris/.claude/session-gates``.

Rather than adding production override knobs (an env-selectable boot command would let a
session point the gate at a stub that always reports COMPLETE), the harness copies the
scripts into ``tmp_path`` and rewrites their two directory constants there. Resolving the
boot wrapper from ``__file__`` is what makes that sufficient: a copied tree gets a stub
wrapper for free, and production keeps no injection point at all.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

BOOT_SRC = Path(__file__).resolve().parents[2] / "norcal" / "boot"

# Reproduces what norcal/boot/claude-boot-context emits, including the detail that makes
# the recovery path load-bearing: it prints a parseable payload and *then* exits 1
# whenever it collected failures (`raise SystemExit(1)`), so every DEGRADED boot hands the
# gate a non-zero returncode alongside a perfectly good payload.
STUB_BOOT_CONTEXT = '''#!/usr/bin/env python3
import json, os, sys

mode = os.environ.get("NORCAL_TEST_BOOT_MODE", "degraded")
if mode == "crash":
    sys.stderr.write("stub wrapper exploded before printing\\n")
    raise SystemExit(3)

complete = mode == "complete"
lines = ["CLAUDE BOOT STATUS: " + ("COMPLETE" if complete else "DEGRADED \\u2014 STOP BEFORE TASK EXECUTION")]
if mode != "no_rules":
    lines.append("LOCAL/CANONICAL CLAUDE.md: OK")
else:
    lines.append("LOCAL/CANONICAL CLAUDE.md: SYNCED_RESTART_REQUIRED")
failures = [] if complete else ["shared boot state is DEGRADED"]
if mode == "two_failures":
    failures = ["shared boot state is DEGRADED", "canonical CLAUDE.md is missing or unreadable"]
lines += ["FAILURE: " + f for f in failures]
preface = "<claude-bootstrap>\\n" + "\\n".join(lines) + "\\n</claude-bootstrap>"

shared = ""
if mode != "no_shared":
    state = "COMPLETE" if complete else "DEGRADED \\u2014 STOP BEFORE TASK EXECUTION"
    shared = "<shared-boot-state>\\nBOOT STATUS: " + state + "\\n</shared-boot-state>\\n\\n"

print(json.dumps({
    "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": shared + preface},
    "systemMessage": "shared boot: " + ("COMPLETE" if complete else "DEGRADED"),
}))
if failures:
    raise SystemExit(1)
'''


def _rewrite_const(text: str, name: str, value: Path) -> str:
    """Repoint a ``NAME = Path('...')`` constant at a temporary directory."""
    pattern = rf"(?m)^{name} = Path\([^)]*\)"
    new, count = re.subn(pattern, f"{name} = Path({str(value)!r})", text)
    assert count == 1, f"expected exactly one {name} assignment to rewrite, found {count}"
    return new


class BootHarness:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "bootdir"
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_dir = tmp_path / "session-gates"
        self.db_path = tmp_path / "kanban.db"

        shutil.copy2(BOOT_SRC / "recovery_boot.py", self.root / "recovery_boot.py")

        gate = (BOOT_SRC / "claude-session-start-gate.py").read_text(encoding="utf-8")
        self.gate = self.root / "claude-session-start-gate.py"
        self.gate.write_text(_rewrite_const(gate, "STATE_DIR", self.state_dir), encoding="utf-8")

        guard = (BOOT_SRC / "claude-tool-guard.py").read_text(encoding="utf-8")
        guard = _rewrite_const(guard, "STATE_DIR", self.state_dir)
        guard = _rewrite_const(guard, "BOOT_DIR", self.root)
        self.guard = self.root / "claude-tool-guard.py"
        self.guard.write_text(guard, encoding="utf-8")

        wrapper = self.root / "claude-boot-context"
        wrapper.write_text(STUB_BOOT_CONTEXT, encoding="utf-8")
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def plant_verify_sentinel(self) -> Path:
        """Put an executable ``verify.py`` in the boot dir that records being run.

        Lets a test prove the gate does not invoke the parity verifier *behaviourally* --
        by observing that nothing executed it -- instead of grepping the gate's source.
        """
        marker = self.root / "verify-was-executed.marker"
        sentinel = self.root / "verify.py"
        sentinel.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed')\n"
            "print('BOOT PARITY: PASS')\n",
            encoding="utf-8",
        )
        sentinel.chmod(sentinel.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return marker

    def forge_state(self, state_dir: Path, session_id: str, **fields) -> Path:
        """Write a gate record that claims a completed boot, for decoy-directory tests."""
        state_dir.mkdir(parents=True, exist_ok=True)
        record = {"session_id": session_id, "complete": True, "recovery_only": False,
                  "recovery_task_id": None}
        record.update(fields)
        path = state_dir / f"{session_id}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def _env(self, extra: dict | None) -> dict:
        env = dict(os.environ)
        # Never inherit a real recovery grant into a test process.
        env.pop("NORCAL_RECOVERY_AUTHORIZED", None)
        env.pop("NORCAL_RECOVERY_TASK_ID", None)
        env["HERMES_KANBAN_DB"] = str(self.db_path)
        env.update({k: str(v) for k, v in (extra or {}).items()})
        return env

    def run_gate(self, session_id: str, *, mode: str = "degraded", env: dict | None = None):
        extra = dict(env or {})
        extra["NORCAL_TEST_BOOT_MODE"] = mode
        proc = subprocess.run(
            [sys.executable, str(self.gate)],
            input=json.dumps({"session_id": session_id, "cwd": "/tmp"}),
            capture_output=True,
            text=True,
            timeout=60,
            env=self._env(extra),
        )
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        ctx = str(payload.get("hookSpecificOutput", {}).get("additionalContext") or "")
        state_file = self.state_dir / f"{session_id}.json"
        state = json.loads(state_file.read_text()) if state_file.exists() else None
        return GateResult(proc.returncode, payload, ctx, state)

    def run_guard(self, session_id: str, *, tool: str = "Bash", tool_input: dict | None = None,
                  env: dict | None = None):
        proc = subprocess.run(
            [sys.executable, str(self.guard)],
            input=json.dumps({
                "session_id": session_id,
                "tool_name": tool,
                "tool_input": tool_input if tool_input is not None else {"command": "echo hello"},
            }),
            capture_output=True,
            text=True,
            timeout=60,
            env=self._env(env),
        )
        out = json.loads(proc.stdout) if proc.stdout.strip() else {}
        decision = str(out.get("hookSpecificOutput", {}).get("permissionDecision") or "")
        reason = str(out.get("hookSpecificOutput", {}).get("permissionDecisionReason") or "")
        return GuardResult(decision, reason)

    def write_card(self, task_id: str, *, status: str = "running", lane: str = "claude_recovery",
                   run_id: int | None = 7, gate: str = "pytest -q tests/norcal"):
        import sqlite3

        con = sqlite3.connect(self.db_path)
        con.execute(
            "CREATE TABLE IF NOT EXISTS tasks "
            "(id TEXT,status TEXT,executor_lane TEXT,recovery_gate_cmd TEXT,current_run_id INTEGER)"
        )
        con.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        con.execute("INSERT INTO tasks VALUES (?,?,?,?,?)", (task_id, status, lane, gate, run_id))
        con.commit()
        con.close()


class GateResult:
    def __init__(self, returncode, payload, ctx, state):
        self.returncode = returncode
        self.payload = payload
        self.ctx = ctx
        self.state = state

    @property
    def recovery_only_block(self) -> bool:
        return "<RECOVERY-ONLY>" in self.ctx

    @property
    def hard_gate_block(self) -> bool:
        return "<HARD-GATE>" in self.ctx

    @property
    def named_gates(self) -> list[str]:
        return [
            line.split("FAILED GATE:", 1)[1].strip().replace("</RECOVERY-ONLY>", "").replace("</HARD-GATE>", "")
            for line in self.ctx.splitlines()
            if line.startswith("FAILED GATE:")
        ]


class GuardResult:
    def __init__(self, decision, reason):
        self.decision = decision
        self.reason = reason

    @property
    def denied(self) -> bool:
        return self.decision == "deny"

    @property
    def allowed(self) -> bool:
        # The guard prints nothing when it has no objection; existing permission policy continues.
        return self.decision == ""


@pytest.fixture
def boot(tmp_path):
    return BootHarness(tmp_path)
