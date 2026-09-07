"""Lessons are retrieved when something fails, never loaded up front.

Christopher's ruling, 2026-09-07: *"no agent should load all the lessons at any
point. that is part of the identifying process when an error is encountered."*

Measured before the change: 196 approved lessons put ~22,600 tokens into every
dispatched task, ahead of the actual work, 196 rules competing for attention.
It is also the wrong shape — a lesson is diagnostic material for a failure that
HAS happened. What binds unconditionally is doctrine, and doctrine loads at
boot.

The first class below is the one that matters. The old code had a
``_CTX_MAX_LESSONS`` cap, which limited the damage but left the mechanism in
place: approving lessons reintroduced bulk loading. So the test is not "few
lessons are injected" but **"lesson text cannot appear in the context at all,
no matter how many are active"** — a property a cap cannot give you.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


@pytest.fixture
def conn(tmp_path: Path):
    db = kb.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


SENTINEL = "NEVERPRELOADED-canary-rule-text"


def _add_lesson(conn, text, *, active=1, applicability="all"):
    import time
    with kb.write_txn(conn):
        cur = conn.execute(
            "INSERT INTO task_lessons (source_task_id, tenant, scope, "
            "applicability, lesson, created_by, created_at, active, state) "
            "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?)",
            ("t_src", kb.LESSON_SCOPE_GLOBAL, applicability, text, "tester",
             int(time.time()), active,
             kb.LESSON_STATE_ACTIVE if active else kb.LESSON_STATE_CANDIDATE),
        )
        return int(cur.lastrowid)


class TestTheCorpusCannotBeLoadedIntoATask:
    def test_lesson_text_never_reaches_the_worker_context(self, conn):
        """The property a cap could not give: not fewer, NONE."""
        for i in range(40):
            _add_lesson(conn, f"{SENTINEL} number {i} do the thing properly")
        tid = kb.create_task(conn, title="work", assignee="worker")

        ctx = kb.build_worker_context(conn, tid)

        assert SENTINEL not in ctx
        assert "Binding verified lessons" not in ctx

    def test_it_points_at_retrieval_instead(self, conn):
        _add_lesson(conn, f"{SENTINEL} something")
        tid = kb.create_task(conn, title="work", assignee="worker")

        ctx = kb.build_worker_context(conn, tid)

        assert "If you hit an error" in ctx
        assert "Do not read them now" in ctx
        assert 'hermes kanban lessons --error' in ctx

    def test_an_empty_corpus_adds_nothing(self, conn):
        tid = kb.create_task(conn, title="work", assignee="worker")
        assert "If you hit an error" not in kb.build_worker_context(conn, tid)


class TestRetrievalMatchesTheErrorSignature:
    RULE = (
        "When a sqlite predicate correlates on a bare column name the inner "
        "table wins the binding, so alias the outer table explicitly."
    )

    def test_it_finds_a_lesson_by_overlapping_terms(self, conn):
        lid = _add_lesson(conn, self.RULE)
        hits = kb.lessons_for_error(
            conn, "sqlite3.OperationalError: ambiguous column name in predicate"
        )
        assert lid in [h["id"] for h in hits]
        assert hits[0]["match_score"] >= 1
        assert "sqlite" in hits[0]["match_terms"]

    def test_ids_paths_and_numbers_do_not_create_false_novelty(self, conn):
        """The same failure twice must match the same lesson.

        Two occurrences differ in ids, paths, line numbers and timestamps.
        Matching raw text would make every recurrence look new — which is
        exactly how a bounded "only a new error opens a loop" rule decays back
        into an unbounded one.
        """
        lid = _add_lesson(conn, self.RULE)
        first = kb.lessons_for_error(
            conn,
            "2026-09-07 10:00:01 sqlite predicate failed at "
            "/home/chris/a/b.py:1841 id=0xdeadbeef",
        )
        second = kb.lessons_for_error(
            conn,
            "2026-09-08 22:13:47 sqlite predicate failed at "
            "/srv/other/zz.py:99 id=0xfeed1234",
        )
        assert [h["id"] for h in first] == [lid]
        assert [h["id"] for h in second] == [lid]

    def test_empty_error_returns_nothing_not_everything(self, conn):
        """A caller that failed to capture the error must get [] — never the
        whole table, which would be bulk loading through the back door."""
        _add_lesson(conn, self.RULE)
        assert kb.lessons_for_error(conn, "") == []
        assert kb.lessons_for_error(conn, "   ") == []
        assert kb.lessons_for_error(conn, "12345 /a/b 0xff") == []

    def test_the_whole_corpus_is_searchable_not_just_bound_rows(self, conn):
        """`active` means "binds unconditionally"; retrieval ignores it.

        A lesson nobody chose to make binding is still the best available
        evidence about an error somebody already hit.
        """
        inactive = _add_lesson(conn, self.RULE, active=0)
        hits = kb.lessons_for_error(conn, "sqlite predicate alias problem")
        assert inactive in [h["id"] for h in hits]

    def test_unrelated_errors_do_not_match(self, conn):
        _add_lesson(conn, self.RULE)
        assert kb.lessons_for_error(
            conn, "TelegramConnectionTimeout while polling updates"
        ) == []

    def test_results_are_ordered_best_first_and_capped(self, conn):
        strong = _add_lesson(
            conn, "sqlite alias predicate column binding inner table"
        )
        _add_lesson(conn, "sqlite alias only")
        hits = kb.lessons_for_error(
            conn, "sqlite alias predicate column binding inner table", limit=5
        )
        assert hits[0]["id"] == strong
        assert hits[0]["match_score"] >= hits[-1]["match_score"]
        assert len(
            kb.lessons_for_error(
                conn, "sqlite alias predicate column", limit=1
            )
        ) == 1

    def test_a_single_shared_word_is_not_a_match(self, conn):
        """Corroboration, not coincidence.

        Live against the real corpus, pure rarity ranked a here-document
        failure against a lesson about clicking unlabelled icons, on the word
        "near" alone — because "near" appeared in exactly one lesson and so
        scored the maximum. One word in common with a paragraph of prose is
        noise, and a confident wrong answer is worse than "no match".
        """
        _add_lesson(
            conn,
            "Hover over an icon and confirm its accessible name before "
            "clicking anything near the toolbar",
        )
        assert kb.lessons_for_error(
            conn, "bash: syntax error near unexpected token"
        ) == []


class TestCliSurface:
    def test_error_lookup_reports_matches(self, kanban_home):
        from hermes_cli import kanban as kc

        with kb.connect_closing() as conn:
            _add_lesson(
                conn,
                "Always alias the outer table in a sqlite correlated predicate",
            )
        out = kc.run_slash('lessons --error "sqlite correlated predicate broke"')
        assert "match that error" in out
        assert "alias" in out

    def test_no_match_tells_the_agent_it_is_a_new_failure(self, kanban_home):
        from hermes_cli import kanban as kc

        with kb.connect_closing() as conn:
            _add_lesson(conn, "something entirely unrelated about telegram")
        out = kc.run_slash('lessons --error "quantum flux capacitor desync"')
        assert "NEW failure" in out
        assert "record it" in out

    def test_json_output(self, kanban_home):
        from hermes_cli import kanban as kc

        with kb.connect_closing() as conn:
            _add_lesson(conn, "alias the outer table in sqlite predicates")
        payload = json.loads(
            kc.run_slash('lessons --error "sqlite predicate alias" --json')
        )
        assert payload and "match_score" in payload[0]


class TestSignatureKeyedLessons:
    """The write half: log the error with the fix, match it exactly next time.

    Term overlap between an error and a paragraph of prose has a hard ceiling
    — a here-document failure cannot match a lesson that says "heredoc",
    because the words differ, and no weighting repairs a vocabulary mismatch.
    Recording the error's signature when the fix is logged removes the guessing
    entirely, and it is the step Christopher's loop already performs.
    """

    ERR = "sqlite3.OperationalError: no such column: id at /srv/x.py:1841"
    FIX = "Alias the outer table before correlating a subquery on a bare column."

    def test_a_recorded_fix_is_found_exactly_next_time(self, conn):
        tid = kb.create_task(conn, title="src", assignee="worker")
        rec = kb.record_error_lesson(
            conn, error_text=self.ERR, lesson=self.FIX, source_task_id=tid,
        )
        # Same failure, different path, line, and timestamp.
        hits = kb.lessons_for_error(
            conn,
            "sqlite3.OperationalError: no such column: id at /opt/other.py:12",
        )
        assert [h["id"] for h in hits] == [rec["id"]]
        assert hits[0]["match_score"] == "exact"

    def test_signature_is_stable_across_occurrences(self, conn):
        a = kb.error_signature("boom at /a/b.py:1 id=0xdead code=42")
        b = kb.error_signature("boom at /z/q.py:999 id=0xbeef code=7")
        assert a == b == "boom code"

    def test_a_recorded_fix_binds_nothing_until_approved(self, conn):
        """Logging a fix must never silently constrain every future task."""
        tid = kb.create_task(conn, title="src", assignee="worker")
        rec = kb.record_error_lesson(
            conn, error_text=self.ERR, lesson=self.FIX, source_task_id=tid,
        )
        row = conn.execute(
            "SELECT active, state FROM task_lessons WHERE id = ?", (rec["id"],)
        ).fetchone()
        assert (row["active"], row["state"]) == (0, kb.LESSON_STATE_CANDIDATE)
        # ...but it is retrievable immediately, which is the point.
        assert kb.lessons_for_error(conn, self.ERR)

    def test_an_error_with_no_signal_is_refused(self, conn):
        tid = kb.create_task(conn, title="src", assignee="worker")
        with pytest.raises(ValueError):
            kb.record_error_lesson(
                conn, error_text="12345 /a/b 0xff", lesson=self.FIX,
                source_task_id=tid,
            )

    def test_legacy_prose_still_reachable_by_overlap(self, conn):
        """Signature-first must not strand the 197 imported rows."""
        legacy = _add_lesson(
            conn, "Alias the outer table in a sqlite correlated predicate"
        )
        hits = kb.lessons_for_error(
            conn, "sqlite correlated predicate alias failure"
        )
        assert legacy in [h["id"] for h in hits]
