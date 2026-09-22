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

def _no_launcher_grant(monkeypatch):
    """Exercise the Kanban authority path, not the launcher short-circuit.

    Since commit c87f2ad84e ("Simplify boot control plane") validate_recovery_task
    returns True immediately when NORCAL_RECOVERY_AUTHORIZED=1. If that variable
    leaks into the test environment these DB-fallback assertions stop testing the
    DB at all, so clear it explicitly rather than relying on a clean env.
    """
    monkeypatch.delenv("NORCAL_RECOVERY_AUTHORIZED", raising=False)


def test_recovery_authority_requires_live_recovery_card(tmp_path, monkeypatch):
    _no_launcher_grant(monkeypatch)
    rb = _load_recovery_boot(); p = _db(tmp_path)
    assert rb.validate_recovery_task("t_recover", p)[0] is True
    p = _db(tmp_path / "done", status="done", run_id=None) if False else p

def test_recovery_authority_rejects_done_or_wrong_lane(tmp_path, monkeypatch):
    _no_launcher_grant(monkeypatch)
    rb = _load_recovery_boot()
    for name, status, lane, run_id in [("done", "done", "claude_recovery", None), ("ordinary", "running", "claude", 8)]:
        d=tmp_path/name; d.mkdir(); p=_db(d, status=status, lane=lane, run_id=run_id)
        ok, _ = rb.validate_recovery_task("t_recover", p)
        assert ok is False

def test_recovery_launchers_bind_task_id_in_child_environment(monkeypatch):
    # Retargeted for the 2026-09-21 boot control-plane redesign (commit c87f2ad84e,
    # "Simplify boot control plane"). The recovery launchers now also export
    # NORCAL_RECOVERY_AUTHORIZED=1, because recovery authority is granted by the
    # launcher before boot rather than resolved from Kanban -- see
    # recovery_boot.validate_recovery_task. The guarantee under test is unchanged:
    # a recovery lane binds the card id into the child env; an ordinary lane
    # execs the binary directly with no env prefix at all.
    monkeypatch.setattr(ex.shutil, "which", lambda name: f"/bin/{name}")
    ca = ex.LAUNCHERS["claude.recovery"].build({"prompt":"repair", "task_id":"t_recover"})
    co = ex.LAUNCHERS["codex.recovery"].build({"prompt":"repair", "task_id":"t_recover"})
    for argv, binary in ((ca, "/bin/claude"), (co, "/bin/codex")):
        assert argv[0] == "/usr/bin/env"
        env_prefix = argv[1:argv.index(binary)]
        assert "NORCAL_RECOVERY_TASK_ID=t_recover" in env_prefix
        assert "NORCAL_RECOVERY_AUTHORIZED=1" in env_prefix
        # The env prefix carries recovery binding and nothing else.
        assert all(a.startswith("NORCAL_RECOVERY_") for a in env_prefix)

    # Ordinary lanes exec the binary directly. This is the counterpart guarantee
    # and the redesign makes it strictly more load-bearing: NORCAL_RECOVERY_AUTHORIZED=1
    # now short-circuits validate_recovery_task, so an ordinary launcher that leaked
    # either variable would hand boot-gate recovery authority to ordinary work.
    for lane in ("claude.headless", "codex.exec"):
        argv = ex.LAUNCHERS[lane].build({"prompt": "normal"})
        assert argv[0] == ("/bin/claude" if lane.startswith("claude") else "/bin/codex")
        assert not any("NORCAL_RECOVERY" in a for a in argv)


def test_shared_boot_complete_requires_exact_state_line():
    rb = _load_recovery_boot()
    assert rb.shared_boot_complete("<shared-boot-state>\nBOOT STATUS: COMPLETE\n</shared-boot-state>") is True
    assert rb.shared_boot_complete("Doctrine example: BOOT STATUS: COMPLETE") is False
    assert rb.shared_boot_complete("BOOT STATUS: DEGRADED — STOP BEFORE TASK EXECUTION\nprose says BOOT STATUS: COMPLETE later") is False


def test_recovery_authority_rejects_missing_gate_and_missing_task(tmp_path, monkeypatch):
    _no_launcher_grant(monkeypatch)
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
