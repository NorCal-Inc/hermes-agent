from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes_cli import exec_supervisor as ex

def _load_recovery_boot():
    import importlib.util
    path = Path(__file__).resolve().parents[2] / "norcal/boot/recovery_boot.py"
    spec = importlib.util.spec_from_file_location("norcal_recovery_boot_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def _db(tmp_path, *, status="running", lane="claude_recovery", run_id=7, gate="check-gate"):
    p = tmp_path / "kanban.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE tasks (id TEXT,status TEXT,executor_lane TEXT,recovery_gate_cmd TEXT,current_run_id INTEGER)")
    c.execute("INSERT INTO tasks VALUES (?,?,?,?,?)", ("t_recover", status, lane, gate, run_id))
    c.commit(); c.close()
    return p

def test_recovery_authority_requires_live_recovery_card(tmp_path):
    rb = _load_recovery_boot(); p = _db(tmp_path)
    assert rb.validate_recovery_task("t_recover", p)[0] is True
    p = _db(tmp_path / "done", status="done", run_id=None) if False else p

def test_recovery_authority_rejects_done_or_wrong_lane(tmp_path):
    rb = _load_recovery_boot()
    for name, status, lane, run_id in [("done", "done", "claude_recovery", None), ("ordinary", "running", "claude", 8)]:
        d=tmp_path/name; d.mkdir(); p=_db(d, status=status, lane=lane, run_id=run_id)
        ok, _ = rb.validate_recovery_task("t_recover", p)
        assert ok is False

def test_recovery_launchers_bind_task_id_in_child_environment(monkeypatch):
    monkeypatch.setattr(ex.shutil, "which", lambda name: f"/bin/{name}")
    ca = ex.LAUNCHERS["claude.recovery"].build({"prompt":"repair", "task_id":"t_recover"})
    co = ex.LAUNCHERS["codex.recovery"].build({"prompt":"repair", "task_id":"t_recover"})
    assert ca[:3] == ["/usr/bin/env", "NORCAL_RECOVERY_TASK_ID=t_recover", "/bin/claude"]
    assert co[:3] == ["/usr/bin/env", "NORCAL_RECOVERY_TASK_ID=t_recover", "/bin/codex"]
    assert ex.LAUNCHERS["claude.headless"].build({"prompt":"normal"})[0] == "/bin/claude"
    assert ex.LAUNCHERS["codex.exec"].build({"prompt":"normal"})[0] == "/bin/codex"


def test_shared_boot_complete_requires_exact_state_line():
    rb = _load_recovery_boot()
    assert rb.shared_boot_complete("<shared-boot-state>\nBOOT STATUS: COMPLETE\n</shared-boot-state>") is True
    assert rb.shared_boot_complete("Doctrine example: BOOT STATUS: COMPLETE") is False
    assert rb.shared_boot_complete("BOOT STATUS: DEGRADED — STOP BEFORE TASK EXECUTION\nprose says BOOT STATUS: COMPLETE later") is False


def test_recovery_authority_rejects_missing_gate_and_missing_task(tmp_path):
    rb = _load_recovery_boot()
    d = tmp_path / "nogate"; d.mkdir()
    p = _db(d, gate="")
    assert rb.validate_recovery_task("t_recover", p)[0] is False
    assert rb.validate_recovery_task("does-not-exist", p)[0] is False


def test_boot_wrappers_use_shared_exact_state_parser():
    root = Path(__file__).resolve().parents[2] / "norcal/boot"
    for name in ("claude-boot-context", "claude-session-start-gate.py", "codex-boot"):
        text = (root / name).read_text(encoding="utf-8")
        assert "shared_boot_complete" in text
    assert '"BOOT STATUS: COMPLETE" not in body' not in (root / "codex-boot").read_text(encoding="utf-8")
