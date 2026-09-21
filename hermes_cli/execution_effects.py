"""Experimental durable execution-effect ledger.

This module is a design spike. It is not wired into the dispatcher.
It tests whether task side effects can carry their inverse durably so a
replacement worker can restore state after the original worker disappears.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable


def ensure_effect_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_effects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            run_id INTEGER,
            effect_type TEXT NOT NULL,
            resource TEXT NOT NULL,
            before_state TEXT,
            after_state TEXT,
            inverse_action TEXT NOT NULL,
            inverse_payload TEXT,
            state TEXT NOT NULL DEFAULT 'prepared',
            created_at INTEGER NOT NULL,
            applied_at INTEGER,
            reverted_at INTEGER,
            committed_at INTEGER
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_effects_run "
        "ON execution_effects(task_id, run_id, state, id)"
    )
    conn.commit()


def prepare_effect(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int | None,
    effect_type: str,
    resource: str,
    before_state: Any,
    after_state: Any,
    inverse_action: str,
    inverse_payload: Any,
) -> int:
    """Durably register the inverse before the external mutation occurs."""
    now = int(time.time())
    cur = conn.execute(
        """
        INSERT INTO execution_effects (
            task_id, run_id, effect_type, resource,
            before_state, after_state, inverse_action, inverse_payload,
            state, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?)
        """,
        (
            task_id,
            run_id,
            effect_type,
            resource,
            json.dumps(before_state),
            json.dumps(after_state),
            inverse_action,
            json.dumps(inverse_payload),
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def mark_applied(conn: sqlite3.Connection, effect_id: int) -> None:
    now = int(time.time())
    cur = conn.execute(
        "UPDATE execution_effects SET state='applied', applied_at=? "
        "WHERE id=? AND state='prepared'",
        (now, effect_id),
    )
    if cur.rowcount != 1:
        raise RuntimeError(f"effect {effect_id} was not prepared")
    conn.commit()


def commit_run(conn: sqlite3.Connection, *, task_id: str, run_id: int | None) -> int:
    now = int(time.time())
    cur = conn.execute(
        "UPDATE execution_effects SET state='committed', committed_at=? "
        "WHERE task_id=? AND run_id IS ? AND state='applied'",
        (now, task_id, run_id),
    )
    conn.commit()
    return int(cur.rowcount)


def _observed_file_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False}
    state: dict[str, Any] = {"exists": True}
    try:
        state["text"] = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        pass
    return state


def _matches_recorded_state(observed: dict[str, Any], recorded: Any) -> bool:
    if not isinstance(recorded, dict):
        return False
    for key, value in recorded.items():
        if observed.get(key) != value:
            return False
    return True


def _apply_inverse(effect: sqlite3.Row) -> None:
    payload = json.loads(effect["inverse_payload"] or "null")
    action = effect["inverse_action"]
    path = Path(effect["resource"])

    if action == "delete_file":
        path.unlink(missing_ok=True)
        return
    if action == "restore_text_file":
        data = payload if isinstance(payload, dict) else {}
        existed = bool(data.get("existed"))
        if existed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(data.get("text", "")), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
        return
    raise ValueError(f"unsupported inverse action: {action}")


def rollback_uncommitted(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int | None,
) -> list[int]:
    """Reverse applied, uncommitted effects in reverse acquisition order."""
    conn.row_factory = sqlite3.Row
    rows: Iterable[sqlite3.Row] = conn.execute(
        "SELECT * FROM execution_effects "
        "WHERE task_id=? AND run_id IS ? AND state IN ('prepared','applied') "
        "ORDER BY id DESC",
        (task_id, run_id),
    ).fetchall()
    reverted: list[int] = []
    for row in rows:
        before = json.loads(row["before_state"] or "null")
        after = json.loads(row["after_state"] or "null")
        observed = _observed_file_state(Path(row["resource"]))
        now = int(time.time())
        if _matches_recorded_state(observed, before):
            # The external action never happened, or was already restored.
            next_state = "reverted"
        elif _matches_recorded_state(observed, after):
            _apply_inverse(row)
            next_state = "reverted"
        else:
            # Never overwrite a state we cannot attribute to this effect.
            next_state = "conflict"
        cur = conn.execute(
            "UPDATE execution_effects SET state=?, reverted_at=? "
            "WHERE id=? AND state IN ('prepared','applied')",
            (next_state, now if next_state == "reverted" else None, int(row["id"])),
        )
        if cur.rowcount == 1 and next_state == "reverted":
            reverted.append(int(row["id"]))
        conn.commit()
    return reverted


def effect_states(conn: sqlite3.Connection, *, task_id: str, run_id: int | None) -> list[str]:
    rows = conn.execute(
        "SELECT state FROM execution_effects WHERE task_id=? AND run_id IS ? ORDER BY id",
        (task_id, run_id),
    ).fetchall()
    return [str(row[0]) for row in rows]


def write_text_with_effect(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int | None,
    path: Path | str,
    text: str,
    fault_after_prepare: bool = False,
    fault_after_mutation: bool = False,
) -> int:
    """Write a UTF-8 text file with a durable inverse registered first.

    The two fault flags are test-only injection points. They model worker death
    immediately after the durable prepare and immediately after the external
    mutation, before ``mark_applied``. Production callers leave both false.
    """
    target = Path(path)
    before = _observed_file_state(target)
    after = {"exists": True, "text": text}
    effect_id = prepare_effect(
        conn,
        task_id=task_id,
        run_id=run_id,
        effect_type="text_file_write",
        resource=str(target),
        before_state=before,
        after_state=after,
        inverse_action="restore_text_file",
        inverse_payload={
            "existed": bool(before.get("exists")),
            "text": before.get("text", ""),
        },
    )
    if fault_after_prepare:
        raise RuntimeError("fault injection after effect prepare")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    if fault_after_mutation:
        raise RuntimeError("fault injection after external mutation")
    mark_applied(conn, effect_id)
    return effect_id
