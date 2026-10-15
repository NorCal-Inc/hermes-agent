"""Worker launch must import the dispatcher's hermes_cli, not the task checkout's.

The worker runs with ``cwd=<task workspace>``. A task project that carries its
own (stale) ``hermes_cli`` package must not shadow the trusted launcher.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from hermes_cli import kanban_db as kb

TRUSTED_ROOT = Path(kb.__file__).resolve().parent.parent


def _shadow_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "task-project"
    pkg = ws / "hermes_cli"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "import pathlib\n"
        "pathlib.Path(__file__).with_name('SHADOW_IMPORTED').write_text('x')\n",
        encoding="utf-8",
    )
    (pkg / "main.py").write_text("def main():\n    raise SystemExit(97)\n\n\nmain()\n", encoding="utf-8")
    return ws


def _clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.pop("PYTHONSAFEPATH", None)
    return env


def test_worker_bootstrap_imports_trusted_package_and_keeps_workspace_cwd(tmp_path):
    ws = _shadow_workspace(tmp_path)
    argv = kb._worker_hermes_argv()
    assert argv[0] == sys.executable and argv[1] == "-c"
    entry = "from hermes_cli.main import main;main()"
    assert entry in argv[2]
    probe = argv[2].replace(
        entry, "import hermes_cli,os;print(hermes_cli.__file__);print(os.getcwd())"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ws, env=_clean_env(), capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    pkg_file, cwd = out.stdout.strip().splitlines()[-2:]
    assert Path(pkg_file).resolve() == TRUSTED_ROOT / "hermes_cli" / "__init__.py"
    assert Path(cwd).resolve() == ws.resolve()
    assert not (ws / "hermes_cli" / "SHADOW_IMPORTED").exists()


def test_plain_dash_m_is_shadowed_but_worker_argv_is_not(tmp_path):
    """Control: proves the fixture really shadows ``-m`` launches."""
    ws = _shadow_workspace(tmp_path)
    shadowed = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main"],
        cwd=ws, env=_clean_env(), capture_output=True, text=True, timeout=60,
    )
    assert shadowed.returncode == 97

    real = subprocess.run(
        [*kb._worker_hermes_argv(), "--version"],
        cwd=ws, env=_clean_env(), capture_output=True, text=True, timeout=120,
    )
    assert real.returncode != 97, real.stderr
    assert not (ws / "hermes_cli" / "SHADOW_IMPORTED").exists() or shadowed.returncode == 97


def test_spawn_uses_trusted_launcher_with_profile_and_workspace(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    (root / "profiles" / "w" / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    # An explicit HERMES_BIN override must not redirect the worker launcher.
    monkeypatch.setenv("HERMES_BIN", str(tmp_path / "wrapper-that-strips-env"))
    ws = _shadow_workspace(tmp_path)

    captured: dict = {}

    class FakeProc:
        pid = 4243

    def fake_popen(cmd, *a, **kw):
        captured.update(cmd=list(cmd), cwd=kw.get("cwd"), env=dict(kw.get("env") or {}))
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    task = kb.Task(
        id="t_trust", title="t", body=None, assignee="w", status="running",
        priority=0, created_by="test", created_at=1, started_at=None,
        completed_at=None, workspace_kind="dir", workspace_path=None,
        claim_lock="lock", claim_expires=None, tenant=None, current_run_id=1,
    )
    kb._default_spawn(task, str(ws))

    cmd = captured["cmd"]
    assert cmd[:2] == [sys.executable, "-c"]
    assert cmd[3:5] == ["-p", "w"]
    assert captured["cwd"] == str(ws)
    assert captured["env"]["TERMINAL_CWD"] == str(ws)
    assert captured["env"]["HERMES_KANBAN_WORKSPACE"] == str(ws)


def test_explicit_hermes_bin_still_honored_for_general_resolution(tmp_path, monkeypatch):
    exe = tmp_path / "custom-hermes"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_BIN", str(exe))
    assert kb._resolve_hermes_argv() == [str(exe)]
