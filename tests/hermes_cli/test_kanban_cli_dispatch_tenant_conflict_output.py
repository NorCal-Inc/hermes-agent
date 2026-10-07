"""``hermes kanban dispatch`` must surface company-lane (tenant) refusals.

``DispatchResult.skipped_tenant_conflict`` is filled by the rule-3 check in
``dispatch_once`` but was never printed by ``_cmd_dispatch`` -- neither in
``--json`` nor in the text report -- so an operator running a dry run saw a
card silently vanish from the tick. These tests pin the output contract.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

import pytest


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Spin up a fresh HERMES_HOME with a clean kanban module graph."""
    test_home = tempfile.mkdtemp(prefix="kanban_cli_tenant_out_")
    os.makedirs(os.path.join(test_home, "profiles", "default"), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    selected = {
        name: module for name, module in list(sys.modules.items())
        if name.startswith("hermes_cli")
        or name.startswith("hermes_state")
        or name == "hermes_constants"
    }
    for name in selected:
        sys.modules.pop(name, None)
    try:
        yield test_home
    finally:
        for name in list(sys.modules):
            if (
                name.startswith("hermes_cli")
                or name.startswith("hermes_state")
                or name == "hermes_constants"
            ):
                sys.modules.pop(name, None)
        sys.modules.update(selected)


def _run_dispatch(monkeypatch, capsys, result, *, as_json):
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {}})
    monkeypatch.setattr(kanban_db, "dispatch_once", lambda conn, **kw: result)
    args = argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=as_json)
    rc = kb_cli._cmd_dispatch(args)
    assert rc == 0
    return capsys.readouterr().out


def _conflict_result():
    from hermes_cli import kanban_db

    res = kanban_db.DispatchResult()
    res.skipped_tenant_conflict.append(
        ("t_conflict1", "acme-worker", "tenant_mismatch")
    )
    return res


def test_json_output_lists_tenant_conflict(isolated_kanban_home, monkeypatch, capsys):
    out = _run_dispatch(monkeypatch, capsys, _conflict_result(), as_json=True)
    payload = json.loads(out)
    assert payload["skipped_tenant_conflict"] == [
        {"task_id": "t_conflict1", "assignee": "acme-worker", "reason": "tenant_mismatch"}
    ]


def test_json_output_empty_list_without_conflict(isolated_kanban_home, monkeypatch, capsys):
    from hermes_cli import kanban_db

    out = _run_dispatch(monkeypatch, capsys, kanban_db.DispatchResult(), as_json=True)
    payload = json.loads(out)
    assert payload["skipped_tenant_conflict"] == []


def test_text_output_prints_tenant_conflict_line(isolated_kanban_home, monkeypatch, capsys):
    out = _run_dispatch(monkeypatch, capsys, _conflict_result(), as_json=False)
    lines = [ln for ln in out.splitlines() if ln.startswith("Skipped (company lane conflict")]
    assert len(lines) == 1
    assert "t_conflict1" in lines[0]
    assert "acme-worker" in lines[0]
    assert "tenant_mismatch" in lines[0]


def test_text_output_silent_without_conflict(isolated_kanban_home, monkeypatch, capsys):
    from hermes_cli import kanban_db

    out = _run_dispatch(monkeypatch, capsys, kanban_db.DispatchResult(), as_json=False)
    assert "company lane conflict" not in out
    # The rest of the report is unchanged by this feature.
    assert "Spawned:      0" in out
