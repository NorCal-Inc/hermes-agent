import sqlite3
from pathlib import Path

from hermes_cli.execution_effects import (
    commit_run,
    effect_states,
    ensure_effect_schema,
    mark_applied,
    prepare_effect,
    rollback_uncommitted,
)


def _db(path: Path):
    conn = sqlite3.connect(path)
    ensure_effect_schema(conn)
    return conn


def test_crashed_worker_can_be_replaced_and_created_file_is_removed(tmp_path):
    db_path = tmp_path / "effects.db"
    resource = tmp_path / "created.txt"
    conn = _db(db_path)
    eid = prepare_effect(
        conn, task_id="t1", run_id=1, effect_type="file_create",
        resource=str(resource), before_state={"exists": False},
        after_state={"exists": True, "text": "worker side effect"}, inverse_action="delete_file",
        inverse_payload=None,
    )
    resource.write_text("worker side effect", encoding="utf-8")
    mark_applied(conn, eid)
    conn.close()  # worker disappears here

    replacement = _db(db_path)
    assert rollback_uncommitted(replacement, task_id="t1", run_id=1) == [eid]
    assert not resource.exists()
    assert effect_states(replacement, task_id="t1", run_id=1) == ["reverted"]


def test_crashed_worker_can_restore_previous_file_contents(tmp_path):
    db_path = tmp_path / "effects.db"
    resource = tmp_path / "config.txt"
    resource.write_text("before", encoding="utf-8")
    conn = _db(db_path)
    eid = prepare_effect(
        conn, task_id="t2", run_id=8, effect_type="file_modify",
        resource=str(resource), before_state={"text": "before"},
        after_state={"text": "after"}, inverse_action="restore_text_file",
        inverse_payload={"existed": True, "text": "before"},
    )
    resource.write_text("after", encoding="utf-8")
    mark_applied(conn, eid)
    conn.close()

    replacement = _db(db_path)
    rollback_uncommitted(replacement, task_id="t2", run_id=8)
    assert resource.read_text(encoding="utf-8") == "before"


def test_committed_effect_is_not_rolled_back(tmp_path):
    db_path = tmp_path / "effects.db"
    resource = tmp_path / "keep.txt"
    conn = _db(db_path)
    eid = prepare_effect(
        conn, task_id="t3", run_id=2, effect_type="file_create",
        resource=str(resource), before_state={"exists": False},
        after_state={"exists": True}, inverse_action="delete_file",
        inverse_payload=None,
    )
    resource.write_text("keep", encoding="utf-8")
    mark_applied(conn, eid)
    assert commit_run(conn, task_id="t3", run_id=2) == 1
    assert rollback_uncommitted(conn, task_id="t3", run_id=2) == []
    assert resource.read_text(encoding="utf-8") == "keep"
    assert effect_states(conn, task_id="t3", run_id=2) == ["committed"]


def test_rollback_is_reverse_order_and_returns_original_state(tmp_path):
    db_path = tmp_path / "effects.db"
    resource = tmp_path / "stack.txt"
    resource.write_text("A", encoding="utf-8")
    conn = _db(db_path)

    first = prepare_effect(
        conn, task_id="t4", run_id=3, effect_type="file_modify",
        resource=str(resource), before_state={"text": "A"},
        after_state={"text": "B"}, inverse_action="restore_text_file",
        inverse_payload={"existed": True, "text": "A"},
    )
    resource.write_text("B", encoding="utf-8")
    mark_applied(conn, first)

    second = prepare_effect(
        conn, task_id="t4", run_id=3, effect_type="file_modify",
        resource=str(resource), before_state={"text": "B"},
        after_state={"text": "C"}, inverse_action="restore_text_file",
        inverse_payload={"existed": True, "text": "B"},
    )
    resource.write_text("C", encoding="utf-8")
    mark_applied(conn, second)

    assert rollback_uncommitted(conn, task_id="t4", run_id=3) == [second, first]
    assert resource.read_text(encoding="utf-8") == "A"
    assert effect_states(conn, task_id="t4", run_id=3) == ["reverted", "reverted"]


def test_crash_between_external_change_and_mark_applied_is_recoverable(tmp_path):
    db_path = tmp_path / "effects.db"
    resource = tmp_path / "half-applied.txt"
    conn = _db(db_path)
    eid = prepare_effect(
        conn, task_id="t5", run_id=4, effect_type="file_create",
        resource=str(resource), before_state={"exists": False},
        after_state={"exists": True, "text": "written"},
        inverse_action="delete_file", inverse_payload=None,
    )
    resource.write_text("written", encoding="utf-8")
    conn.close()  # dies before mark_applied

    replacement = _db(db_path)
    assert rollback_uncommitted(replacement, task_id="t5", run_id=4) == [eid]
    assert not resource.exists()
    assert effect_states(replacement, task_id="t5", run_id=4) == ["reverted"]


def test_recovery_refuses_to_overwrite_unattributed_later_change(tmp_path):
    db_path = tmp_path / "effects.db"
    resource = tmp_path / "conflict.txt"
    resource.write_text("before", encoding="utf-8")
    conn = _db(db_path)
    eid = prepare_effect(
        conn, task_id="t6", run_id=5, effect_type="file_modify",
        resource=str(resource), before_state={"exists": True, "text": "before"},
        after_state={"exists": True, "text": "ours"},
        inverse_action="restore_text_file",
        inverse_payload={"existed": True, "text": "before"},
    )
    resource.write_text("ours", encoding="utf-8")
    mark_applied(conn, eid)
    conn.close()

    # A later actor changes the resource. Recovery must not clobber it.
    resource.write_text("someone-else", encoding="utf-8")
    replacement = _db(db_path)
    assert rollback_uncommitted(replacement, task_id="t6", run_id=5) == []
    assert resource.read_text(encoding="utf-8") == "someone-else"
    assert effect_states(replacement, task_id="t6", run_id=5) == ["conflict"]
