"""Read-only kanban CLI works where ``kanban.db.init.lock`` cannot be opened.

``_cross_process_init_lock`` used to call ``lock_path.open("a+b")`` outside
its try block. In a read-only filesystem context (the Codex verifier sandbox
mounts ``~/.hermes`` read-only) that raised ``OSError(EROFS)`` and every
``hermes kanban`` command — including pure reads such as ``show`` and
``attachments`` — failed with "could not initialize database". A plain
``sqlite3`` ``mode=ro`` read of the same board worked.

Covered here:

1. Lock open raising EROFS / EACCES → ``connect()`` proceeds without the
   cross-process lock and a read works on an already-initialised board.
2. Uninitialised board + read-only → one clear ``OperationalError`` (the CLI
   wrapper prints it as a single line), nothing cached as initialised.
3. The normal writable path is unchanged: the lock is really acquired for the
   duration of the block and released afterwards, and the writability
   preflight still runs.
"""

from __future__ import annotations

import errno
import logging
import os
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb._INIT_LOCK_UNAVAILABLE_WARNED.clear()
    yield home
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb._INIT_LOCK_UNAVAILABLE_WARNED.clear()


def _fail_lock_open(monkeypatch, err: int) -> None:
    """Make ``Path.open`` raise ``OSError(err)`` for ``*.init.lock`` only.

    Everything else (header probe, sqlite) opens normally, so the test
    isolates exactly the failure the sandbox produces: the lock file is the
    one thing on the read-only path that must be opened for write.
    """
    real_open = Path.open

    def fake_open(self, *args, **kwargs):
        if self.name.endswith(".init.lock"):
            raise OSError(err, os.strerror(err), str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)


def _create_task(db_path: Path, task_id: str = "t_readonly") -> None:
    with kb.connect_closing(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, body, status, created_at) "
            "VALUES (?, ?, '', 'todo', 1)",
            (task_id, "read-only probe"),
        )
        conn.commit()


@pytest.mark.parametrize("err", [errno.EROFS, errno.EACCES, errno.EPERM])
def test_lock_unopenable_connect_reads_initialised_board(kanban_home, monkeypatch, caplog, err):
    """Lock open failing with a no-write-access errno → connect proceeds
    (read-only mode) and a read against an initialised board works."""
    db_path = kb.kanban_db_path(board="default")
    _create_task(db_path)
    resolved = str(db_path.resolve())
    kb._INITIALIZED_PATHS.discard(resolved)

    _fail_lock_open(monkeypatch, err)
    with caplog.at_level(logging.WARNING, logger=kb._log.name):
        with kb.connect_closing(db_path) as conn:
            row = conn.execute(
                "SELECT title FROM tasks WHERE id = ?", ("t_readonly",)
            ).fetchone()
        assert row is not None and row["title"] == "read-only probe"
        assert resolved in kb._INITIALIZED_PATHS

        # Second connect in the same process (fast path) still reads.
        kb._INITIALIZED_PATHS.discard(resolved)
        with kb.connect_closing(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1

    warnings = [r for r in caplog.records if "cannot be opened" in r.getMessage()]
    assert len(warnings) == 1, "lock-unavailable must be logged once per process, not per connect"
    assert warnings[0].levelno == logging.WARNING


def test_lock_open_other_oserror_still_propagates(kanban_home, monkeypatch):
    """Only the no-write-access errnos are tolerated; anything else is a real fault."""
    db_path = kb.kanban_db_path(board="default")
    _create_task(db_path)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    _fail_lock_open(monkeypatch, errno.EIO)
    with pytest.raises(OSError) as excinfo:
        kb.connect(db_path)
    assert excinfo.value.errno == errno.EIO


def test_readonly_missing_board_fails_with_clear_error(kanban_home, monkeypatch):
    """No DB file + read-only → one clear OperationalError, nothing cached."""
    db_path = kb.kanban_db_path(board="default")
    assert not db_path.exists()
    _fail_lock_open(monkeypatch, errno.EROFS)

    with pytest.raises(sqlite3.OperationalError) as excinfo:
        kb.connect(db_path)
    msg = str(excinfo.value)
    assert "does not exist" in msg and "not writable" in msg
    assert "\n" not in msg
    assert str(db_path.resolve()) not in kb._INITIALIZED_PATHS
    # Nothing was created on the "read-only" side.
    assert not db_path.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_readonly_directory_real_eacces_missing_board(kanban_home):
    """Real EACCES (directory 0o555, no monkeypatch): same clear error path."""
    db_path = kb.kanban_db_path(board="default")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    mode = db_path.parent.stat().st_mode
    os.chmod(db_path.parent, 0o555)
    try:
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            kb.connect(db_path)
        assert "does not exist" in str(excinfo.value)
    finally:
        os.chmod(db_path.parent, mode)


def test_readonly_stale_schema_fails_with_clear_error(kanban_home, monkeypatch):
    """Board exists but needs a migration + read-only → clear error, no init."""
    db_path = kb.kanban_db_path(board="default")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(str(db_path))
    try:
        legacy.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT)")
        legacy.commit()
    finally:
        legacy.close()
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    _fail_lock_open(monkeypatch, errno.EACCES)
    with pytest.raises(sqlite3.OperationalError) as excinfo:
        kb.connect(db_path)
    msg = str(excinfo.value)
    assert "schema is not current" in msg and "not writable" in msg
    assert "\n" not in msg
    assert str(db_path.resolve()) not in kb._INITIALIZED_PATHS
    # The legacy file was not migrated behind the caller's back.
    probe = sqlite3.connect(str(db_path))
    try:
        cols = {r[1] for r in probe.execute("PRAGMA table_info(tasks)")}
    finally:
        probe.close()
    assert cols == {"id", "title"}


def test_schema_is_current_true_on_fully_initialised_board(kanban_home):
    db_path = kb.kanban_db_path(board="default")
    kb.connect(db_path).close()
    with kb.connect_closing(db_path) as conn:
        assert kb._schema_is_current(conn) is True


def test_writable_path_unchanged_lock_acquired_and_released(kanban_home):
    """Normal context: the lock yields False, is held for the block, released after."""
    import fcntl

    db_path = kb.kanban_db_path(board="default")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = db_path.with_name(db_path.name + ".init.lock")

    def _try_lock() -> bool:
        with lock_path.open("a+b") as h:
            try:
                fcntl.flock(h.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            fcntl.flock(h.fileno(), fcntl.LOCK_UN)
            return True

    with kb._cross_process_init_lock(db_path) as unavailable:
        assert unavailable is False
        assert lock_path.exists()
        # flock is per open-file-description: a fresh handle in this same
        # process must see the lock as taken while the block runs.
        assert _try_lock() is False, "lock not actually held during the block"
    assert _try_lock() is True, "lock not released after the block"


def test_writable_path_still_runs_preflight_and_readonly_skips_it(kanban_home, monkeypatch):
    import hermes_state

    calls: list[Path] = []
    real = hermes_state.preflight_db_writability

    def recording(path, **kwargs):
        calls.append(Path(path))
        return real(path, **kwargs)

    monkeypatch.setattr(hermes_state, "preflight_db_writability", recording)
    db_path = kb.kanban_db_path(board="default")
    kb.connect(db_path).close()
    assert len(calls) == 1, "writable first-connect must still run the preflight"

    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    _fail_lock_open(monkeypatch, errno.EROFS)
    kb.connect(db_path).close()
    assert len(calls) == 1, "read-only mode must not run the writability preflight"
