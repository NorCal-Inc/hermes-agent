"""An inherited ``HERMES_KANBAN_DB`` must not drag a re-homed process back to the live board.

Incident 2026-10-12 22:02 (card t_6cc051f2, audit EVT-PROBECLEAN-20261012): a worker's
probe script set ``HERMES_HOME`` to a temp folder and called ``kb.init_db()`` /
``kb.create_task()``. The worker environment still carried the dispatcher's
``HERMES_KANBAN_DB`` pin, which ``kanban_db_path()`` honoured ahead of ``HERMES_HOME``,
so five probe cards landed on the live board, were dispatched, and reached the
dashboard ledger. ``tests/conftest.py`` strips the pin for pytest; ad-hoc scripts had
no protection.

The rule under test is narrow on purpose: only a pin **inside** the platform-native
Hermes root (``~/.hermes``) combined with a kanban home **outside** it is treated as
inherited and ignored. Every shape the dispatcher→worker handoff produces, every
custom / Docker root, and every test that pins a tempfile keeps the historical
"pin wins" behaviour.

``Path.home()`` is redirected to ``tmp_path`` in every test here so the "native root"
is a throwaway directory. Nothing in this module can resolve to the operator's real
``~/.hermes`` (the autouse conftest guard would refuse the write anyway).
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


def _task_count(db: Path) -> int:
    if not db.exists():
        return 0
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        try:
            return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        except sqlite3.OperationalError:
            return 0
    finally:
        conn.close()


@pytest.fixture
def native_root(tmp_path, monkeypatch):
    """A throwaway ``~/.hermes`` standing in for the platform-native root.

    Returns the root; the live board ``A`` lives at ``<root>/kanban.db`` and is
    initialised empty so a stray write is visible as a row count change.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
    ):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / ".hermes"
    root.mkdir()
    kb._INITIALIZED_PATHS.clear()
    kb._FOREIGN_PIN_MEMO = None
    live = root / "kanban.db"
    kb.connect(db_path=live).close()
    assert _task_count(live) == 0
    return root


def test_inherited_pin_is_ignored_when_process_rehomes_to_temp(native_root, tmp_path, monkeypatch, caplog):
    """The incident shape: pin -> live board A, HERMES_HOME -> temp home B, create_task.

    Before the fix the card lands on A. After it the card lands on B's own
    ``kanban.db`` and A is untouched, and the ignored pin is logged once.
    """
    live = native_root / "kanban.db"
    temp_home = tmp_path / "probe-home"
    temp_home.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(live))
    monkeypatch.setenv("HERMES_HOME", str(temp_home))

    assert kb.kanban_db_path() == temp_home / "kanban.db"

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        kb.init_db()
        with kb.connect() as conn:
            task_id = kb.create_task(conn, title="probe card", assignee="dev")
        # Second resolution must not warn again.
        kb.kanban_db_path()

    assert _task_count(live) == 0, "inherited pin wrote the probe card to the live board"
    assert _task_count(temp_home / "kanban.db") == 1
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).title == "probe card"
    warnings = [r for r in caplog.records if "ignoring inherited HERMES_KANBAN_DB" in r.getMessage()]
    assert len(warnings) == 1
    assert str(live) in warnings[0].getMessage()


def test_dispatcher_handoff_pin_is_honoured_for_profile_worker(native_root, monkeypatch):
    """A worker spawned by the dispatcher: pin under the root, HERMES_HOME under
    ``<root>/profiles/<name>``. The pin wins exactly as before, for every board
    argument."""
    live = native_root / "kanban.db"
    profile_home = native_root / "profiles" / "orion_worker"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(live))
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    assert kb.kanban_db_path() == live
    assert kb.kanban_db_path(board="some-other-board") == live
    with kb.connect() as conn:
        kb.create_task(conn, title="worker card", assignee="dev")
    assert _task_count(live) == 1


def test_dispatcher_handoff_pin_is_honoured_through_symlinked_board(native_root, monkeypatch):
    """The live install reaches its board through ``<root>/kanban.db -> kanban-db/kanban.db``
    and pins the real file. A pin that is only *physically* under the root still counts."""
    real = native_root / "kanban-db" / "kanban.db"
    real.parent.mkdir()
    (native_root / "kanban.db").unlink()
    (native_root / "kanban.db").symlink_to(Path("kanban-db") / "kanban.db")
    kb.connect(db_path=real).close()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(real))
    monkeypatch.setenv("HERMES_HOME", str(native_root))

    assert kb.kanban_db_path() == real


def test_pin_outside_native_root_still_wins(native_root, tmp_path, monkeypatch):
    """Custom / Docker roots and tests that pin a tempfile: nothing changes."""
    docker_root = tmp_path / "opt" / "hermes"
    (docker_root / "profiles" / "coder").mkdir(parents=True)
    pin = docker_root / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pin))
    monkeypatch.setenv("HERMES_HOME", str(docker_root / "profiles" / "coder"))
    assert kb.kanban_db_path() == pin

    # Pin and home in unrelated temp dirs (the shape of the existing
    # ``test_env_var_db_override_still_wins``): still honoured.
    forced = tmp_path / "custom.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(forced))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "elsewhere"))
    assert kb.kanban_db_path() == forced
    assert kb.kanban_db_path(board="ignored") == forced


def test_kanban_home_override_restores_live_pin_on_purpose(native_root, tmp_path, monkeypatch):
    """The documented escape hatch: a re-homed process that *means* to use the
    live board sets ``HERMES_KANBAN_HOME`` to the root, and the pin is honoured."""
    live = native_root / "kanban.db"
    temp_home = tmp_path / "probe-home"
    temp_home.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(live))
    monkeypatch.setenv("HERMES_HOME", str(temp_home))
    assert kb.kanban_db_path() == temp_home / "kanban.db"

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(native_root))
    assert kb.kanban_db_path() == live


def test_no_pin_resolves_through_hermes_home(native_root, tmp_path, monkeypatch):
    """Control: with no pin at all, a temp HERMES_HOME already resolved to its own
    board before this change."""
    temp_home = tmp_path / "probe-home"
    temp_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(temp_home))
    assert kb.kanban_db_path() == temp_home / "kanban.db"
