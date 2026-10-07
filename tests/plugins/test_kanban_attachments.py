"""Tests for Kanban task file attachments (#35338).

Covers three layers:
  * ``hermes_cli.kanban_db`` accessors (add/list/get/delete + path helpers)
  * the dashboard REST surface (upload / list / download / delete)
  * worker-context surfacing so a kanban worker sees the absolute paths

The plugin router is attached to a bare FastAPI app — same approach as
``test_kanban_dashboard_plugin.py`` — so we exercise the real HTTP path
(multipart upload, streaming download) without the whole dashboard.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_attach_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _make_task(conn, title="t") -> str:
    return kb.create_task(conn, title=title)


# ---------------------------------------------------------------------------
# DB-layer accessors
# ---------------------------------------------------------------------------


def test_add_list_get_delete_attachment(kanban_home, tmp_path):
    conn = kb.connect()
    try:
        task_id = _make_task(conn)
        # Write a real blob under the per-task dir so delete can unlink it.
        dest_dir = kb.task_attachments_dir(task_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        blob = dest_dir / "source.pdf"
        blob.write_bytes(b"%PDF-1.4 fake")

        att_id = kb.add_attachment(
            conn,
            task_id,
            filename="source.pdf",
            stored_path=str(blob),
            content_type="application/pdf",
            size=blob.stat().st_size,
            uploaded_by="tester",
        )
        assert att_id > 0

        atts = kb.list_attachments(conn, task_id)
        assert len(atts) == 1
        a = atts[0]
        assert a.filename == "source.pdf"
        assert a.content_type == "application/pdf"
        assert a.size == len(b"%PDF-1.4 fake")
        assert a.uploaded_by == "tester"
        assert a.stored_path == str(blob)

        got = kb.get_attachment(conn, att_id)
        assert got is not None and got.id == att_id

        # Attachments are append-only history until the card is archived.
        assert kb.archive_task(conn, task_id)
        removed = kb.delete_attachment(conn, att_id)
        assert removed is not None and removed.id == att_id
        assert kb.list_attachments(conn, task_id) == []
        assert not blob.exists(), "delete should unlink the on-disk blob"
        assert kb.get_attachment(conn, att_id) is None
        assert [e.kind for e in kb.list_events(conn, task_id)][-1] == "attachment_removed"
    finally:
        conn.close()


def test_delete_attachment_missing_returns_none(kanban_home):
    conn = kb.connect()
    try:
        assert kb.delete_attachment(conn, 999999) is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Rule 8 parity — attachments on a live card are evidence and cannot be
# deleted; the card must be archived first (same gate as delete_task).
# ---------------------------------------------------------------------------


def _attach_blob(conn, task_id: str, name: str = "evidence.txt") -> tuple[int, Path]:
    dest_dir = kb.task_attachments_dir(task_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    blob = dest_dir / name
    blob.write_bytes(b"evidence bytes")
    att_id = kb.add_attachment(
        conn, task_id, filename=name, stored_path=str(blob),
        content_type="text/plain", size=blob.stat().st_size,
    )
    return att_id, blob


def test_delete_attachment_on_live_card_is_refused_and_recorded(kanban_home):
    with kb.connect_closing() as conn:
        task_id = _make_task(conn, title="live card")
        att_id, blob = _attach_blob(conn, task_id)
        events_before = len(kb.list_events(conn, task_id))

        live_status = kb.get_task(conn, task_id).status
        assert live_status != "archived"

        with pytest.raises(kb.AttachmentDeleteRefused) as excinfo:
            kb.delete_attachment(conn, att_id)
        assert excinfo.value.attachment.id == att_id
        assert excinfo.value.status == live_status
        assert "archive" in str(excinfo.value)

        # Row and blob both survive.
        assert kb.get_attachment(conn, att_id) is not None
        assert blob.exists(), "refusal must not unlink the on-disk blob"
        assert blob.read_bytes() == b"evidence bytes"

        # The refusal itself is on the record, and only the refusal.
        events = kb.list_events(conn, task_id)
        assert len(events) == events_before + 1
        last = events[-1]
        assert last.kind == "attachment_delete_refused"
        assert last.payload["reason"] == "task_not_archived"
        assert last.payload["status"] == live_status
        assert last.payload["attachment_id"] == att_id
        assert last.payload["filename"] == "evidence.txt"


@pytest.mark.usefixtures("all_assignees_spawnable")
def test_delete_attachment_refused_on_done_card_too(kanban_home):
    """``done`` is not ``archived``: a completed card still holds its evidence."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="done card", assignee="worker")
        att_id, blob = _attach_blob(conn, task_id)
        assert kb.claim_task(conn, task_id) is not None
        assert kb.complete_task(conn, task_id, result="done")
        with pytest.raises(kb.AttachmentDeleteRefused) as excinfo:
            kb.delete_attachment(conn, att_id)
        assert excinfo.value.status == "done"
        assert kb.get_attachment(conn, att_id) is not None
        assert blob.exists()


def test_delete_attachment_on_archived_card_is_allowed(kanban_home):
    with kb.connect_closing() as conn:
        task_id = _make_task(conn, title="archived card")
        att_id, blob = _attach_blob(conn, task_id)
        assert kb.archive_task(conn, task_id)

        removed = kb.delete_attachment(conn, att_id)
        assert removed is not None and removed.id == att_id
        assert kb.get_attachment(conn, att_id) is None
        assert not blob.exists()
        kinds = [e.kind for e in kb.list_events(conn, task_id)]
        assert kinds[-1] == "attachment_removed"
        assert "attachment_delete_refused" not in kinds


def test_attachment_delete_refused_is_not_lifecycle_progress():
    assert "attachment_delete_refused" in kb._GAUNTLET_STALE_NONPROGRESS_EVENT_KINDS


def test_attachments_root_is_per_board(kanban_home, monkeypatch):
    # default board uses <root>/kanban/attachments
    default_root = kb.attachments_root(board="default")
    assert default_root.name == "attachments"
    # a named board nests under its board dir
    monkeypatch.delenv("HERMES_KANBAN_ATTACHMENTS_ROOT", raising=False)
    named = kb.attachments_root(board="default")
    assert named == default_root


# ---------------------------------------------------------------------------
# Worker context surfacing
# ---------------------------------------------------------------------------


def test_worker_context_lists_attachments_with_absolute_path(kanban_home):
    conn = kb.connect()
    try:
        task_id = _make_task(conn, title="translate PDF")
        dest_dir = kb.task_attachments_dir(task_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        blob = dest_dir / "manual.pdf"
        blob.write_bytes(b"data")
        kb.add_attachment(
            conn,
            task_id,
            filename="manual.pdf",
            stored_path=str(blob.resolve()),
            content_type="application/pdf",
            size=4,
        )
        ctx = kb.build_worker_context(conn, task_id)
        assert "## Attachments" in ctx
        assert "manual.pdf" in ctx
        # The absolute path must appear so the worker can read_file it.
        assert str(blob.resolve()) in ctx
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# REST surface — upload / list / download / delete round-trip
# ---------------------------------------------------------------------------


def _create_task_via_api(client) -> str:
    r = client.post("/api/plugins/kanban/tasks", json={"title": "x"})
    assert r.status_code == 200, r.text
    return r.json()["task"]["id"]


def test_upload_list_download_delete_roundtrip(client):
    task_id = _create_task_via_api(client)
    content = b"hello attachment world"

    # Upload
    r = client.post(
        f"/api/plugins/kanban/tasks/{task_id}/attachments",
        files={"file": ("notes.txt", content, "text/plain")},
    )
    assert r.status_code == 200, r.text
    att = r.json()["attachment"]
    assert att["filename"] == "notes.txt"
    assert att["size"] == len(content)
    att_id = att["id"]

    # List (drawer also embeds it in GET /tasks/:id)
    r = client.get(f"/api/plugins/kanban/tasks/{task_id}/attachments")
    assert r.status_code == 200
    assert [a["filename"] for a in r.json()["attachments"]] == ["notes.txt"]

    detail = client.get(f"/api/plugins/kanban/tasks/{task_id}").json()
    assert "attachments" in detail
    assert len(detail["attachments"]) == 1

    # Download streams the exact bytes back
    r = client.get(f"/api/plugins/kanban/attachments/{att_id}")
    assert r.status_code == 200
    assert r.content == content

    # Delete on a live card is refused (409) — attachments are append-only
    # task history until the card is archived — and the bytes survive.
    r = client.delete(f"/api/plugins/kanban/attachments/{att_id}")
    assert r.status_code == 409, r.text
    assert "archive" in r.json()["detail"]
    assert client.get(f"/api/plugins/kanban/attachments/{att_id}").content == content

    # After archiving, delete removes the row and the file
    conn = kb.connect()
    try:
        assert kb.archive_task(conn, task_id)
    finally:
        conn.close()
    r = client.delete(f"/api/plugins/kanban/attachments/{att_id}")
    assert r.status_code == 200, r.text
    assert client.get(f"/api/plugins/kanban/attachments/{att_id}").status_code == 404
    assert client.get(
        f"/api/plugins/kanban/tasks/{task_id}/attachments"
    ).json()["attachments"] == []


def test_upload_sanitizes_traversal_filename(client):
    task_id = _create_task_via_api(client)
    r = client.post(
        f"/api/plugins/kanban/tasks/{task_id}/attachments",
        files={"file": ("../../../../etc/passwd", b"x", "text/plain")},
    )
    assert r.status_code == 200, r.text
    stored_path = r.json()["attachment"]["stored_path"]
    # The leaf name only; never escapes the per-task attachments dir.
    assert Path(stored_path).name == "passwd"
    task_dir = kb.task_attachments_dir(task_id).resolve()
    assert Path(stored_path).resolve().is_relative_to(task_dir)


def test_download_unknown_attachment_404(client):
    assert client.get("/api/plugins/kanban/attachments/424242").status_code == 404


# ---------------------------------------------------------------------------
# Shared helper — store_attachment_bytes (used by dashboard + tool + CLI)
# ---------------------------------------------------------------------------


def test_store_attachment_bytes_roundtrip(kanban_home):
    conn = kb.connect()
    try:
        task_id = _make_task(conn)
        att_id = kb.store_attachment_bytes(
            conn, task_id, "doc.txt", b"some bytes",
            content_type="text/plain", uploaded_by="tester",
        )
        a = kb.get_attachment(conn, att_id)
        assert a is not None
        assert a.filename == "doc.txt"
        assert a.size == len(b"some bytes")
        assert a.uploaded_by == "tester"
        assert Path(a.stored_path).read_bytes() == b"some bytes"
        assert Path(a.stored_path).resolve().is_relative_to(
            kb.task_attachments_dir(task_id).resolve()
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI — hermes kanban attach / attachments / attach-rm
# ---------------------------------------------------------------------------


def test_cli_attach_attachments_and_rm(kanban_home, tmp_path):
    from hermes_cli.kanban import run_slash

    conn = kb.connect()
    try:
        task_id = _make_task(conn, title="cli-attach")
    finally:
        conn.close()

    src = tmp_path / "upload.txt"
    src.write_bytes(b"cli file body")

    out = run_slash(f"attach {task_id} {src}")
    assert "Attached" in out, out

    conn = kb.connect()
    try:
        atts = kb.list_attachments(conn, task_id)
        assert len(atts) == 1
        att_id = atts[0].id
        assert atts[0].filename == "upload.txt"
        assert Path(atts[0].stored_path).read_bytes() == b"cli file body"
    finally:
        conn.close()

    listed = run_slash(f"attachments {task_id}")
    assert "upload.txt" in listed

    # Live card: attach-rm refuses, says what to do, leaves row + blob.
    refused = run_slash(f"attach-rm {att_id}")
    assert "refused" in refused and "archive the card first" in refused, refused
    assert "Deleted attachment" not in refused
    conn = kb.connect()
    try:
        atts = kb.list_attachments(conn, task_id)
        assert [a.id for a in atts] == [att_id]
        assert Path(atts[0].stored_path).read_bytes() == b"cli file body"
        assert [e.kind for e in kb.list_events(conn, task_id)][-1] == "attachment_delete_refused"
    finally:
        conn.close()

    # Archived card: attach-rm deletes.
    archived = run_slash(f"archive {task_id}")
    assert "rchived" in archived, archived
    removed = run_slash(f"attach-rm {att_id}")
    assert "Deleted attachment" in removed, removed
    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, task_id) == []
    finally:
        conn.close()


def test_cli_attach_rm_unknown_id_still_no_such_attachment(kanban_home):
    from hermes_cli.kanban import run_slash

    out = run_slash("attach-rm 999999")
    assert "no such attachment: 999999" in out, out


def test_cli_attach_rm_returns_nonzero_on_live_card(kanban_home, monkeypatch, capsys):
    """The argparse handler itself returns 1 on refusal (run_slash hides rc)."""
    import argparse
    from hermes_cli.kanban import _cmd_attach_rm

    with kb.connect_closing() as conn:
        task_id = _make_task(conn, title="rc check")
        att_id, blob = _attach_blob(conn, task_id)

    rc = _cmd_attach_rm(argparse.Namespace(attachment_id=att_id))
    assert rc == 1
    err = capsys.readouterr().err
    assert "archive the card first" in err and task_id in err
    assert blob.exists()

    with kb.connect_closing() as conn:
        assert kb.archive_task(conn, task_id)
    rc = _cmd_attach_rm(argparse.Namespace(attachment_id=att_id))
    assert rc == 0
    assert not blob.exists()


