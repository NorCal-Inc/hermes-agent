"""A nested ``hermes`` launched from a worker's shell must not own the worker's run.

Reproduces incident t_85883f17 run 5421: a worker ran a diagnostic
``env -u HERMES_DELEGATED_CHILD_CONTEXT hermes -z 'Reply with exactly: probe ok'``;
the child inherited HERMES_KANBAN_TASK/RUN_ID and called kanban_block on the
parent's run.  Real subprocesses are used because the guard is process lineage.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE = textwrap.dedent(
    """
    import json, sys
    from agent.delegation_context import is_inherited_kanban_lineage
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    res = kt._handle_block({"reason": "only probe ok", "kind": "needs_input"})
    print(json.dumps({"inherited": is_inherited_kanban_lineage(),
                      "mode": kt._check_kanban_mode(),
                      "res": res}))
    """
)


def _setup(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("HERMES_")},
        "HERMES_HOME": str(home),
        "PYTHONPATH": str(_REPO_ROOT),
    }
    code = (
        "from hermes_cli import kanban_db as kb;kb.init_db();c=kb.connect();"
        "t=kb.create_task(c,title='p',assignee=None,workspace_kind='scratch');"
        "kb.claim_task(c,t);r=kb.latest_run(c,t).id;print(t,r)"
    )
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, check=True, cwd=tmp_path).stdout.split()
    tid, run = out[-2], out[-1]
    env.update(HERMES_KANBAN_TASK=tid, HERMES_KANBAN_RUN_ID=run)
    return env, tid


def _status(env, tid, cwd):
    clean = {k: v for k, v in env.items() if not k.startswith("HERMES_KANBAN_")}
    code = (f"from hermes_cli import kanban_db as kb;c=kb.connect();"
            f"print(kb.get_task(c,{tid!r}).status)")
    return subprocess.run([sys.executable, "-c", code], env=clean, cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.split()[-1]


def _run(env, tid, cwd, via_nested):
    probe = _PROBE.replace("TID", tid)
    if via_nested:
        # worker -> shell command -> nested hermes; the marker is stripped,
        # exactly like the original `env -u HERMES_DELEGATED_CHILD_CONTEXT`.
        wrapper = (
            "import subprocess,sys,os;e=dict(os.environ);"
            "e.pop('HERMES_DELEGATED_CHILD_CONTEXT',None);"
            f"r=subprocess.run([sys.executable,'-c',{probe!r}],env=e,capture_output=True,text=True);"
            "print(r.stdout);print(r.stderr,file=sys.stderr)"
        )
        argv = [sys.executable, "-c", wrapper]
    else:
        argv = [sys.executable, "-c", probe]
    p = subprocess.run(argv, env=env, capture_output=True, text=True, cwd=cwd)
    lines = [l for l in p.stdout.splitlines() if l.startswith("{")]
    assert lines, (p.stdout[-800:], p.stderr[-800:])
    line = lines[-1]
    return json.loads(line)


def test_nested_child_cannot_block_parent_run(tmp_path):
    env, tid = _setup(tmp_path)
    out = _run(env, tid, tmp_path, via_nested=True)
    assert out["inherited"] is True
    assert out["mode"] is False
    assert "refused" in out["res"]
    assert _status(env, tid, tmp_path) == "running"


def test_owning_worker_still_blocks_its_own_run(tmp_path):
    env, tid = _setup(tmp_path)
    out = _run(env, tid, tmp_path, via_nested=False)
    assert out["inherited"] is False
    assert out["mode"] is True
    assert _status(env, tid, tmp_path) == "blocked"


def test_worker_shell_kanban_cli_still_works(tmp_path):
    """Narrow scope: the CLI/DB layer is unchanged for a worker's own shell."""
    env, tid = _setup(tmp_path)
    code = ("from hermes_cli import kanban_db as kb;c=kb.connect();"
            f"print(kb.get_task(c,{tid!r}).status)")
    p = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp_path,
                       capture_output=True, text=True)
    assert p.stdout.split()[-1] == "running", p.stderr[-400:]
