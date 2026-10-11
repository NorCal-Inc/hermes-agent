"""``norcal/boot/hermes-shared-boot-context`` must honour ``HERMES_KANBAN_DB``.

The dispatcher pins every worker's board with ``HERMES_KANBAN_DB`` and the worker
sandbox binds only that board's directory. ``hermes_cli.norcal_boot`` runs the
shared boot generator at every fresh-session start with the worker's env, so the
generator's Kanban-continuity read must resolve the board through the pin, not
through a hard-coded ``<root>/.hermes/kanban.db`` (t_650bbeaf: strace of a worker
startup showed the generator opening the live board while the pin named a
different, non-existent file).

Both boards below carry a sentinel running task; the payload must show the
pinned board's sentinel, cite the pinned path as ``source=``, and never read or
create the default board's files.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

BOOT_SRC = Path(__file__).resolve().parents[2] / "norcal" / "boot"

_SCHEMA = (
    "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT,"
    " priority INTEGER, created_at INTEGER, started_at INTEGER, completed_at INTEGER,"
    " current_run_id INTEGER, last_heartbeat_at INTEGER, last_failure_error TEXT);"
    "CREATE TABLE task_links (parent_id TEXT, child_id TEXT);"
    "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, status TEXT, outcome TEXT,"
    " summary TEXT, error TEXT, started_at INTEGER, ended_at INTEGER);"
)


def _board(path: Path, task_id: str, title: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)
    now = int(time.time())
    con.execute(
        "INSERT INTO tasks (id,title,assignee,status,priority,created_at,started_at,"
        "current_run_id,last_heartbeat_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, title, "default", "running", 2, now, now, 1, now),
    )
    con.commit()
    con.close()
    return path


class _BootCopy:
    """Copy of the generator rooted at a tmp ``CH`` (same rewrite as
    test_production_roles_registry) so its default board path is a tmp path we
    can prove untouched."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "ch"
        self.boot = self.root / "boot"
        self.boot.mkdir(parents=True)
        shutil.copy2(BOOT_SRC / "boot-constraints.md", self.boot / "boot-constraints.md")
        src = (BOOT_SRC / "hermes-shared-boot-context").read_text(encoding="utf-8")
        new, count = re.subn(r"(?m)^CH = Path\([^)]*\)", f"CH = Path({str(self.root)!r})", src)
        assert count == 1, f"expected exactly one CH assignment to rewrite, found {count}"
        self.script = self.boot / "hermes-shared-boot-context"
        self.script.write_text(new, encoding="utf-8")
        for gate in ("skill-trust", "production-roles"):
            d = self.root / ".hermes" / "security" / gate
            d.mkdir(parents=True, exist_ok=True)
            p = d / "verify.py"
            p.write_text('#!/usr/bin/env python3\nprint("stub PASS")\n', encoding="utf-8")
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
        self.default_db = self.root / ".hermes" / "kanban.db"

    def run(self, pin: Path | None) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN")}
        env.pop("HERMES_HOME", None)
        if pin is not None:
            env["HERMES_KANBAN_DB"] = str(pin)
        p = subprocess.run(
            [sys.executable, str(self.script)],
            capture_output=True, text=True, timeout=120, cwd=str(self.root), env=env,
        )
        assert "<shared-boot-state>" in p.stdout, p.stderr
        return p.stdout


def _continuity(payload: str) -> str:
    return payload.split("KANBAN CONTINUITY", 1)[1].split("</shared-boot-state>", 1)[0]


def test_pinned_board_is_read_and_default_board_is_never_touched(tmp_path):
    boot = _BootCopy(tmp_path)
    _board(boot.default_db, "t_default0", "DEFAULT-BOARD-SENTINEL")
    default_before = {p.name: p.stat().st_mtime_ns for p in boot.default_db.parent.glob("kanban.db*")}
    pinned = _board(tmp_path / "pinned-dir" / "kanban.db", "t_pinned00", "PINNED-BOARD-SENTINEL")

    body = _continuity(boot.run(pin=pinned))

    assert f"source={pinned}" in body, body
    assert "t_pinned00" in body and "PINNED-BOARD-SENTINEL" in body, body
    assert "t_default0" not in body and "DEFAULT-BOARD-SENTINEL" not in body, body
    # No -wal/-shm sidecar was created and the default file was not modified:
    # the default board was never opened.
    default_after = {p.name: p.stat().st_mtime_ns for p in boot.default_db.parent.glob("kanban.db*")}
    assert default_after == default_before == {"kanban.db": default_before["kanban.db"]}


def test_pin_to_missing_file_reports_unavailable_without_falling_back(tmp_path):
    boot = _BootCopy(tmp_path)
    _board(boot.default_db, "t_default0", "DEFAULT-BOARD-SENTINEL")
    missing = tmp_path / "nowhere" / "kanban.db"

    payload = boot.run(pin=missing)

    assert "KANBAN CONTINUITY: UNAVAILABLE" in payload, payload
    assert "t_default0" not in payload, payload
    assert not missing.exists() and not missing.parent.exists()
    assert sorted(p.name for p in boot.default_db.parent.glob("kanban.db*")) == ["kanban.db"]


def test_without_pin_the_default_board_is_still_used(tmp_path):
    boot = _BootCopy(tmp_path)
    _board(boot.default_db, "t_default0", "DEFAULT-BOARD-SENTINEL")

    body = _continuity(boot.run(pin=None))

    assert f"source={boot.default_db}" in body, body
    assert "t_default0" in body, body
