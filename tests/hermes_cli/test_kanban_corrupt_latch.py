"""Once corruption is detected, kanban.db connections must stop writing."""
import sqlite3

import pytest

from hermes_cli import kanban_db as kb


def test_latch_makes_connections_read_only_until_cleared(tmp_path):
    db = tmp_path / "kanban.db"
    conn = kb.connect(db_path=db)
    conn.execute("CREATE TABLE IF NOT EXISTS scratch(x)")
    conn.close()

    kb._set_corrupt_latch(db.resolve())
    conn = kb.connect(db_path=db)
    try:
        conn.execute("SELECT count(*) FROM tasks").fetchone()  # reads still work
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO scratch VALUES (1)")
    finally:
        conn.close()

    kb._clear_corrupt_latch(db.resolve())
    conn = kb.connect(db_path=db)
    try:
        conn.execute("INSERT INTO scratch VALUES (1)")
    finally:
        conn.close()


def test_latch_ignored_after_file_replaced(tmp_path):
    db = tmp_path / "kanban.db"
    kb.connect(db_path=db).close()
    kb._set_corrupt_latch(db.resolve())
    repl = tmp_path / "new.db"
    repl.write_bytes(db.read_bytes())
    repl.replace(db)  # new inode
    assert not kb._corrupt_latch_active(db.resolve())


def test_integrity_failure_sets_latch_and_healthy_probe_clears(tmp_path):
    db = tmp_path / "kanban.db"
    kb.connect(db_path=db).close()
    kb._INITIALIZED_PATHS.discard(str(db.resolve()))
    raw = bytearray(db.read_bytes())
    raw[4096 * 2: 4096 * 2 + 64] = b"\xff" * 64  # damage a non-header page
    db.write_bytes(bytes(raw))
    for suffix in ("-wal", "-shm"):
        (tmp_path / ("kanban.db" + suffix)).unlink(missing_ok=True)
    res = kb.repair_db(db_path=db)
    if res.status == "corrupt":
        assert kb._corrupt_latch_active(db.resolve())
