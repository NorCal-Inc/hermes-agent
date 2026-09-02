#!/usr/bin/env python3
"""Mechanical authority check for boot-gate recovery executors.

Recovery authority exists only while the named Kanban card is an actively running
claude_recovery task with a deterministic recovery gate. This module grants no
ordinary-task authority and contains no model-facing policy decisions.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
import sqlite3
from typing import Tuple

DEFAULT_DB = Path("/home/chris/.hermes/kanban.db")
RECOVERY_LANE = "claude_recovery"


def shared_boot_complete(text: str) -> bool:
    """Return True only when the canonical shared state line declares COMPLETE.

    A prose mention of ``BOOT STATUS: COMPLETE`` elsewhere in doctrine or context
    must never satisfy the startup gate.
    """
    return bool(re.search(r"(?m)^BOOT STATUS: COMPLETE\s*$", text or ""))


def recovery_task_id() -> str:
    return (os.environ.get("NORCAL_RECOVERY_TASK_ID") or "").strip()


def validate_recovery_task(task_id: str, db_path: str | Path | None = None) -> Tuple[bool, str]:
    task_id = (task_id or "").strip()
    if not task_id:
        return False, "recovery task id missing"
    path = Path(db_path or os.environ.get("HERMES_KANBAN_DB") or DEFAULT_DB)
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT id,status,executor_lane,recovery_gate_cmd,current_run_id "
            "FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        con.close()
    except Exception as exc:
        return False, f"recovery authority database unavailable: {type(exc).__name__}: {exc}"
    if row is None:
        return False, f"recovery task {task_id!r} not found"
    if row["executor_lane"] != RECOVERY_LANE:
        return False, f"task {task_id} is not in {RECOVERY_LANE} lane"
    if row["status"] != "running" or row["current_run_id"] is None:
        return False, f"recovery task {task_id} is not actively running"
    if not (row["recovery_gate_cmd"] or "").strip():
        return False, f"recovery task {task_id} has no deterministic gate"
    return True, "authorized active recovery task"
