"""The mistake log feeds task_lessons -- as candidates, and only one way.

Two halves of one system that never touched: ``task_lessons`` is enforced
(``build_worker_context`` injects active lessons as binding constraints) but was
empty; ``Jobs/mistake-logging.md`` is populated but nothing reads it. These pin
the bridge, and specifically pin the properties that stop it becoming a
self-feeding loop.
"""

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "import_mistake_log_lessons.py"


def _load():
    spec = importlib.util.spec_from_file_location("import_mistake_log_lessons", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


imp = _load()


HEADER = """# Mistake logging

| Date | What went wrong | Rule that prevents it | How it was resolved | Full account |
|---|---|---|---|---|
"""


def _log(tmp_path: Path, *rows: str) -> Path:
    p = tmp_path / "mistake-logging.md"
    p.write_text(HEADER + "\n".join(rows) + "\n")
    return p


def _board(tmp_path: Path) -> Path:
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE task_lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_task_id TEXT NOT NULL,
            tenant TEXT,
            scope TEXT NOT NULL,
            applicability TEXT NOT NULL,
            lesson TEXT NOT NULL,
            evidence TEXT,
            verification_id INTEGER,
            regression_proof_id INTEGER,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            retired_at INTEGER, retired_by TEXT, retired_reason TEXT,
            state TEXT NOT NULL DEFAULT 'active',
            review_by INTEGER, retire_condition TEXT
        )"""
    )
    conn.commit()
    conn.close()
    return db


ROW = "| 2026-09-07 | **Something broke.** | **Check X before Y.** | Fixed by Z. | `note.md` |"


class TestParsing:
    def test_extracts_the_rule_column(self, tmp_path):
        rows = imp.parse_rows(_log(tmp_path, ROW))
        assert len(rows) == 1
        assert rows[0]["rule"] == "Check X before Y."
        assert rows[0]["resolution"] == "Fixed by Z."
        assert rows[0]["date"] == "2026-09-07"

    def test_unescaped_pipes_in_content_do_not_shift_columns(self, tmp_path):
        """Four rows in the live file contain `env | grep`.

        A naive ``.split('|')`` reads the wrong cell as the rule and would
        import garbage as a binding-candidate rule.
        """
        row = (
            "| 2026-09-07 | **Ran `env | grep -i hermes`.** "
            "| **Never dump the environment.** | Used cut -d= -f1. | `note.md` |"
        )
        rows = imp.parse_rows(_log(tmp_path, row))
        assert len(rows) == 1
        assert rows[0]["rule"] == "Never dump the environment."

    def test_rows_without_a_rule_are_skipped(self, tmp_path):
        rows = imp.parse_rows(_log(tmp_path, "| 2026-09-07 | broke | | | `n.md` |"))
        assert rows == []

    def test_non_row_lines_are_ignored(self, tmp_path):
        rows = imp.parse_rows(_log(tmp_path, "## a heading", "some prose", ROW))
        assert len(rows) == 1

    def test_long_rules_are_truncated_under_the_kernel_limit(self, tmp_path):
        long_rule = "Do the thing. " * 400
        row = f"| 2026-09-07 | what | {long_rule} | r | `n.md` |"
        rows = imp.parse_rows(_log(tmp_path, row))
        assert len(rows[0]["rule"]) <= imp.MAX_RULE_CHARS + 8
        assert len(rows[0]["rule"]) < 4000  # LESSON_MAX_CHARS


class TestImportedRowsBindNothing:
    """The safety property. A mistake-log row has no verified source task, so
    importing it as binding would launder an unverified claim into canon."""

    def test_every_imported_row_is_an_inactive_candidate(self, tmp_path):
        log, db = _log(tmp_path, ROW), _board(tmp_path)
        assert imp.main(["--log", str(log), "--db", str(db), "--apply"]) == 0

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM task_lessons").fetchall()
        assert len(rows) == 1
        assert rows[0]["active"] == 0
        assert rows[0]["state"] == "candidate"
        assert rows[0]["created_by"] == imp.CREATED_BY

    def test_provenance_is_visible_and_not_a_fake_task_id(self, tmp_path):
        log, db = _log(tmp_path, ROW), _board(tmp_path)
        imp.main(["--log", str(log), "--db", str(db), "--apply"])
        conn = sqlite3.connect(db)
        sid = conn.execute("SELECT source_task_id FROM task_lessons").fetchone()[0]
        assert sid.startswith(imp.PROVENANCE_PREFIX)
        assert not sid.startswith("t_"), "must not be confusable with a real card"

    def test_evidence_records_where_it_came_from(self, tmp_path):
        log, db = _log(tmp_path, ROW), _board(tmp_path)
        imp.main(["--log", str(log), "--db", str(db), "--apply"])
        conn = sqlite3.connect(db)
        ev = json.loads(conn.execute("SELECT evidence FROM task_lessons").fetchone()[0])
        assert ev["origin"] == "Jobs/mistake-logging.md"
        assert ev["logged_date"] == "2026-09-07"
        assert ev["resolution"] == "Fixed by Z."


class TestNotALoop:
    def test_rerunning_imports_nothing_new(self, tmp_path):
        """Idempotent, so it is safe to run after every vault edit."""
        log, db = _log(tmp_path, ROW), _board(tmp_path)
        imp.main(["--log", str(log), "--db", str(db), "--apply"])
        imp.main(["--log", str(log), "--db", str(db), "--apply"])
        imp.main(["--log", str(log), "--db", str(db), "--apply"])
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM task_lessons").fetchone()[0] == 1

    def test_identical_rules_collapse_to_one_candidate(self, tmp_path):
        """The same imperative logged twice is one rule, not two."""
        log = _log(tmp_path, ROW, ROW.replace("2026-09-07", "2026-09-05"))
        db = _board(tmp_path)
        imp.main(["--log", str(log), "--db", str(db), "--apply"])
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM task_lessons").fetchone()[0] == 1

    def test_dry_run_writes_nothing(self, tmp_path):
        log, db = _log(tmp_path, ROW), _board(tmp_path)
        assert imp.main(["--log", str(log), "--db", str(db)]) == 0
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM task_lessons").fetchone()[0] == 0

    def test_never_writes_back_to_the_mistake_log(self, tmp_path):
        """One direction only: an imported lesson can never re-enter as a row."""
        log, db = _log(tmp_path, ROW), _board(tmp_path)
        before = log.read_bytes()
        imp.main(["--log", str(log), "--db", str(db), "--apply"])
        assert log.read_bytes() == before

    def test_empty_log_refuses_rather_than_succeeding_quietly(self, tmp_path):
        log = tmp_path / "empty.md"
        log.write_text(HEADER)
        assert imp.main(["--log", str(log), "--db", str(_board(tmp_path))]) == 1


class TestMissingInputs:
    @pytest.mark.parametrize("which", ["log", "db"])
    def test_missing_path_exits_nonzero(self, tmp_path, which):
        log = _log(tmp_path, ROW)
        db = _board(tmp_path)
        args = ["--log", str(log), "--db", str(db)]
        args[args.index("--" + which) + 1] = str(tmp_path / "nope")
        assert imp.main(args) == 2
