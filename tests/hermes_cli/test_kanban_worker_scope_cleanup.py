"""stop_finished_worker_scopes: stop scopes of ended runs, never running ones."""
import sqlite3
import subprocess

import hermes_cli.kanban_db as kdb


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, ended_at INTEGER, worker_pid INTEGER)")
    conn.executemany(
        "INSERT INTO task_runs VALUES (?,?,?,?)",
        [(10, "t_done0001", 1700000000, 111), (11, "t_live0001", None, 222), (12, "t_lingr001", 1700000000, 333)],
    )
    return conn


def _fake(monkeypatch, listing, alive):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        out = listing if "list-units" in argv else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(kdb.os, "name", "posix")
    monkeypatch.setattr(kdb.shutil, "which", lambda n: "/usr/bin/systemctl")
    monkeypatch.setattr(kdb.subprocess, "run", run)
    monkeypatch.setattr(kdb, "_pid_alive", lambda pid: pid in alive)
    return calls


LISTING = (
    "hermes-kanban-t_done0001-10-0a1b2c3d.scope loaded active running x\n"
    "hermes-kanban-t_live0001-11-0a1b2c3e.scope loaded active running x\n"
    "hermes-kanban-t_lingr001-12-0a1b2c3f.scope loaded active running x\n"
    "hermes-kanban-t_other001-99-0a1b2c40.scope loaded active running x\n"
    "tmux-spawn-abc.scope loaded active running x\n"
)


def test_stops_only_ended_runs_with_dead_worker(monkeypatch):
    calls = _fake(monkeypatch, LISTING, alive={222, 333})
    stopped = kdb.stop_finished_worker_scopes(_conn())
    assert stopped == ["hermes-kanban-t_done0001-10-0a1b2c3d.scope"]
    acted = [c for c in calls if "list-units" not in c]
    assert all(c[-1] == stopped[0] for c in acted)
    assert ["stop", "reset-failed"] == [c[2] for c in acted]


def test_task_id_must_match_run_row(monkeypatch):
    listing = "hermes-kanban-t_wrong001-10-0a1b2c3d.scope loaded active running x\n"
    _fake(monkeypatch, listing, alive=set())
    assert kdb.stop_finished_worker_scopes(_conn()) == []


def test_no_systemctl_is_noop(monkeypatch):
    monkeypatch.setattr(kdb.os, "name", "posix")
    monkeypatch.setattr(kdb.shutil, "which", lambda n: None)
    assert kdb.stop_finished_worker_scopes(_conn()) == []
