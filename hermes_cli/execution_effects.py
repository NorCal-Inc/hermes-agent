"""Durable ownership for reversible task-run side effects.

The kanban task lifecycle already knows whether a run completed, paused for a
resumable dependency, or lost ownership.  This module gives that lifecycle a
small durable ledger for external side effects so the control plane does not
have to guess what a dead worker changed.

The ledger deliberately does not create a second recovery protocol.  A run
owns effects while it is active.  Existing run outcomes route those effects:

* success / review handoff -> committed
* dependency or provider wait -> held, then adopted by the next run
* crash / timeout / reclaim / spawn failure -> rollback_pending

Rollback is compare-before-write.  If current state is neither the recorded
before nor after state, the effect becomes ``conflict`` and Hermes must not
overwrite a newer unattributed change.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


_ACTIVE_STATES = ("prepared", "applied")
_UNRESOLVED_STATES = ("rollback_pending", "conflict")


@dataclass(frozen=True)
class EffectToken:
    db_path: str
    effect_id: int


def ensure_effect_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    """Create or migrate the execution-effect ledger idempotently."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_effects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            run_id INTEGER,
            origin_run_id INTEGER,
            effect_type TEXT NOT NULL,
            resource TEXT NOT NULL,
            before_state TEXT,
            after_state TEXT,
            inverse_action TEXT NOT NULL,
            inverse_payload TEXT,
            state TEXT NOT NULL DEFAULT 'prepared',
            hold_reason TEXT,
            created_at INTEGER NOT NULL,
            applied_at INTEGER,
            held_at INTEGER,
            rollback_requested_at INTEGER,
            reverted_at INTEGER,
            conflict_at INTEGER,
            committed_at INTEGER
        )
        """
    )
    cols = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(execution_effects)").fetchall()
    }
    additions = {
        "origin_run_id": "origin_run_id INTEGER",
        "hold_reason": "hold_reason TEXT",
        "held_at": "held_at INTEGER",
        "rollback_requested_at": "rollback_requested_at INTEGER",
        "conflict_at": "conflict_at INTEGER",
    }
    added_origin = False
    for name, ddl in additions.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE execution_effects ADD COLUMN {ddl}")
            if name == "origin_run_id":
                added_origin = True
    if added_origin:
        conn.execute(
            "UPDATE execution_effects SET origin_run_id = run_id "
            "WHERE origin_run_id IS NULL AND run_id IS NOT NULL"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_effects_run "
        "ON execution_effects(task_id, run_id, state, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_effects_state "
        "ON execution_effects(state, task_id, id)"
    )
    if commit:
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
    commit: bool = True,
    ensure_schema: bool = True,
) -> int:
    """Durably register the inverse before the external mutation occurs."""
    if ensure_schema:
        ensure_effect_schema(conn, commit=False)
    now = int(time.time())
    cur = conn.execute(
        """
        INSERT INTO execution_effects (
            task_id, run_id, origin_run_id, effect_type, resource,
            before_state, after_state, inverse_action, inverse_payload,
            state, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?)
        """,
        (
            task_id,
            run_id,
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
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def mark_applied(
    conn: sqlite3.Connection,
    effect_id: int,
    *,
    commit: bool = True,
) -> None:
    now = int(time.time())
    cur = conn.execute(
        "UPDATE execution_effects SET state='applied', applied_at=? "
        "WHERE id=? AND state='prepared'",
        (now, effect_id),
    )
    if cur.rowcount != 1:
        raise RuntimeError(f"effect {effect_id} was not prepared")
    if commit:
        conn.commit()


def commit_run(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int | None,
    commit: bool = True,
) -> int:
    now = int(time.time())
    cur = conn.execute(
        "UPDATE execution_effects SET state='committed', committed_at=?, "
        "hold_reason=NULL, held_at=NULL "
        "WHERE task_id=? AND run_id IS ? "
        "AND state IN ('prepared','applied','held')",
        (now, task_id, run_id),
    )
    if commit:
        conn.commit()
    return int(cur.rowcount)


def hold_run(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int | None,
    reason: str,
    commit: bool = True,
) -> int:
    now = int(time.time())
    cur = conn.execute(
        "UPDATE execution_effects SET state='held', hold_reason=?, held_at=? "
        "WHERE task_id=? AND run_id IS ? AND state IN ('prepared','applied')",
        (reason, now, task_id, run_id),
    )
    if commit:
        conn.commit()
    return int(cur.rowcount)


def request_rollback_run(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int | None,
    commit: bool = True,
) -> int:
    now = int(time.time())
    cur = conn.execute(
        "UPDATE execution_effects SET state='rollback_pending', "
        "rollback_requested_at=? "
        "WHERE task_id=? AND run_id IS ? AND state IN ('prepared','applied')",
        (now, task_id, run_id),
    )
    if commit:
        conn.commit()
    return int(cur.rowcount)


def route_run_effects(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int | None,
    policy: str,
    reason: Optional[str] = None,
    commit: bool = True,
) -> int:
    """Route one closed run through commit, hold, or rollback semantics."""
    if run_id is None:
        return 0
    if policy == "commit":
        return commit_run(conn, task_id=task_id, run_id=run_id, commit=commit)
    if policy == "hold":
        return hold_run(
            conn,
            task_id=task_id,
            run_id=run_id,
            reason=reason or "resumable interruption",
            commit=commit,
        )
    if policy == "rollback":
        return request_rollback_run(
            conn, task_id=task_id, run_id=run_id, commit=commit
        )
    raise ValueError(f"unknown effect policy: {policy}")


def adopt_held_effects(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    new_run_id: int,
    commit: bool = True,
) -> int:
    """Transfer resumable partial effects to the replacement run."""
    now = int(time.time())
    cur = conn.execute(
        "UPDATE execution_effects SET state='applied', run_id=?, "
        "applied_at=COALESCE(applied_at, ?), hold_reason=NULL, held_at=NULL "
        "WHERE task_id=? AND state='held'",
        (int(new_run_id), now, task_id),
    )
    if commit:
        conn.commit()
    return int(cur.rowcount)


def unresolved_effect_count(conn: sqlite3.Connection, *, task_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM execution_effects "
        "WHERE task_id=? AND state IN ('rollback_pending','conflict')",
        (task_id,),
    ).fetchone()
    return int(row[0]) if row else 0


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
    """Backward-compatible direct rollback used by the original spike tests."""
    conn.row_factory = sqlite3.Row
    conn.execute(
        "UPDATE execution_effects SET state='rollback_pending', rollback_requested_at=? "
        "WHERE task_id=? AND run_id IS ? AND state IN ('prepared','applied')",
        (int(time.time()), task_id, run_id),
    )
    reverted, _conflicts = reconcile_pending_rollbacks(
        conn, task_id=task_id, commit=True
    )
    return reverted


def reconcile_pending_rollbacks(
    conn: sqlite3.Connection,
    *,
    task_id: str | None = None,
    commit: bool = True,
) -> tuple[list[int], list[int]]:
    """Resolve rollback_pending effects safely, in reverse acquisition order."""
    conn.row_factory = sqlite3.Row
    params: tuple[Any, ...] = ()
    where = "state='rollback_pending'"
    if task_id is not None:
        where += " AND task_id=?"
        params = (task_id,)
    rows: Iterable[sqlite3.Row] = conn.execute(
        f"SELECT * FROM execution_effects WHERE {where} "
        "ORDER BY task_id, id DESC",
        params,
    ).fetchall()
    reverted: list[int] = []
    conflicts: list[int] = []
    for row in rows:
        before = json.loads(row["before_state"] or "null")
        after = json.loads(row["after_state"] or "null")
        observed = _observed_file_state(Path(row["resource"]))
        now = int(time.time())
        if _matches_recorded_state(observed, before):
            next_state = "reverted"
        elif _matches_recorded_state(observed, after):
            _apply_inverse(row)
            restored = _observed_file_state(Path(row["resource"]))
            next_state = "reverted" if _matches_recorded_state(restored, before) else "conflict"
        else:
            next_state = "conflict"
        if next_state == "reverted":
            cur = conn.execute(
                "UPDATE execution_effects SET state='reverted', reverted_at=? "
                "WHERE id=? AND state='rollback_pending'",
                (now, int(row["id"])),
            )
            if cur.rowcount == 1:
                reverted.append(int(row["id"]))
        else:
            cur = conn.execute(
                "UPDATE execution_effects SET state='conflict', conflict_at=? "
                "WHERE id=? AND state='rollback_pending'",
                (now, int(row["id"])),
            )
            if cur.rowcount == 1:
                conflicts.append(int(row["id"]))
        if commit:
            conn.commit()
    if commit:
        conn.commit()
    return reverted, conflicts


def effect_states(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int | None,
) -> list[str]:
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
    """Write a UTF-8 text file with a durable inverse registered first."""
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


def _workspace_target(path: str) -> Path | None:
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    run_id = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    db_path = (os.environ.get("HERMES_KANBAN_DB") or "").strip()
    workspace = (os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
    if not (task_id and run_id and db_path and workspace):
        return None
    try:
        root = Path(workspace).expanduser().resolve(strict=False)
        target = Path(path).expanduser().resolve(strict=False)
        target.relative_to(root)
    except (OSError, ValueError):
        return None
    return target


def prepare_workspace_text_effect(path: str, text: str) -> EffectToken | None:
    """Register a local workspace text write for the active kanban run.

    Only task-scoped writes inside ``HERMES_KANBAN_WORKSPACE`` participate.
    System files, cross-profile paths, and non-kanban sessions keep their
    existing behavior until they have a purpose-built reversible adapter.
    """
    target = _workspace_target(path)
    if target is None:
        return None
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    run_id = int((os.environ.get("HERMES_KANBAN_RUN_ID") or "0").strip())
    db_path = (os.environ.get("HERMES_KANBAN_DB") or "").strip()
    before = _observed_file_state(target)
    after = {"exists": True, "text": text}
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        effect_id = prepare_effect(
            conn,
            task_id=task_id,
            run_id=run_id,
            effect_type="workspace_text_write",
            resource=str(target),
            before_state=before,
            after_state=after,
            inverse_action="restore_text_file",
            inverse_payload={
                "existed": bool(before.get("exists")),
                "text": before.get("text", ""),
            },
            commit=True,
            ensure_schema=False,
        )
    finally:
        conn.close()
    return EffectToken(db_path=db_path, effect_id=effect_id)


def mark_workspace_effect_applied(token: EffectToken | None) -> None:
    if token is None:
        return
    conn = sqlite3.connect(token.db_path, timeout=10)
    try:
        mark_applied(conn, token.effect_id, commit=True)
    finally:
        conn.close()


def cancel_workspace_effect(token: EffectToken | None) -> None:
    """Close a prepared effect when the external write itself never happened."""
    if token is None:
        return
    conn = sqlite3.connect(token.db_path, timeout=10)
    try:
        conn.execute(
            "UPDATE execution_effects SET state='reverted', reverted_at=? "
            "WHERE id=? AND state='prepared'",
            (int(time.time()), int(token.effect_id)),
        )
        conn.commit()
    finally:
        conn.close()
