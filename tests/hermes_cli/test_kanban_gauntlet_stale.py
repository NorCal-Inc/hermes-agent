"""Gauntlet freshness: enforced work that was parked mid-chain and abandoned.

The lifecycle tests next door prove the chain cannot be SKIPPED. These prove
the complementary failure is visible: a card handed to verification that no
verifier ever claims, a failed verdict whose repair never happens, an enforced
card nobody ever picks up. Enforcement without freshness only converts silent
false completion into silent indefinite parking.

Two properties are pinned down here, and the second matters as much as the
first:

* DETECTION — parked-and-forgotten Gauntlet work is found deterministically,
  once per episode, with the facts (age, status, verification state,
  regression debt, last meaningful event) attached to an auditable event.
* NON-MUTATION — the scan changes NOTHING. It does not complete, verify,
  unblock, requeue or reassign. A scanner has no evidence, and a scanner that
  manufactured a transition would defeat the exact chain it is watching.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.config_defaults import DEFAULT_CONFIG


TIMEOUT = 4 * 3600
REALERT = 4 * 3600


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _events(conn, tid, kind=None):
    rows = conn.execute(
        "SELECT kind, payload, created_at FROM task_events "
        "WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    out = [
        (r["kind"], json.loads(r["payload"]) if r["payload"] else None)
        for r in rows
    ]
    if kind is not None:
        out = [e for e in out if e[0] == kind]
    return out


def _age(conn, tid, seconds: int) -> None:
    """Move every timestamp on a task ``seconds`` into the past.

    Equivalent to letting ``seconds`` elapse, but deterministic: shifting the
    board back is the same as moving the clock forward, and it works for the
    dispatcher path too (which reads ``time.time()`` internally).
    """
    conn.execute(
        "UPDATE tasks SET created_at = created_at - ?, "
        "started_at = CASE WHEN started_at IS NULL THEN NULL "
        "                  ELSE started_at - ? END "
        "WHERE id = ?",
        (seconds, seconds, tid),
    )
    conn.execute(
        "UPDATE task_events SET created_at = created_at - ? WHERE task_id = ?",
        (seconds, tid),
    )
    conn.commit()


def _scan(conn, **kw):
    kw.setdefault("stale_timeout_seconds", TIMEOUT)
    kw.setdefault("realert_seconds", REALERT)
    return kb.detect_stale_gauntlet_work(conn, **kw)


def _ids(entries):
    return [e.task_id for e in entries]


def _snapshot(conn, tid) -> dict:
    """Every column of the task row, for the non-mutation assertions."""
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    return {k: row[k] for k in row.keys()}


def _review_pending(conn, title="handed off, never verified") -> str:
    """A card sitting in VERIFICATION_PENDING with no verifier in sight."""
    tid = kb.create_task(conn, title=title, assignee="default", gauntlet=True)
    claimed = kb.claim_task(conn, tid)
    kb.request_review(
        conn, tid, summary="impl", reviewer="default",
        expected_run_id=claimed.current_run_id,
    )
    task = kb.get_task(conn, tid)
    assert (task.status, task.verification_state) == (
        "review", kb.VERIFICATION_PENDING,
    )
    return tid


def _review_failed(conn, title="failed, parked in review") -> str:
    """A FAILED verdict left in the review lane (no routing)."""
    tid = _review_pending(conn, title=title)
    ok, detail = kb.record_verification(
        conn, tid, passed=False, verifier="reviewer", reason="gate red",
        route_on_failure=False,
    )
    assert (ok, detail) == (True, kb.VERIFICATION_FAILED)
    return tid


def _rework_owed(conn, title="failed, repair never done") -> str:
    """A failed verdict routed to repair: regression debt, nobody working."""
    tid = _review_pending(conn, title=title)
    ok, detail = kb.record_verification(
        conn, tid, passed=False, verifier="reviewer", reason="3 tests fail",
    )
    assert (ok, detail) == (True, "rework")
    task = kb.get_task(conn, tid)
    assert task.status in ("ready", "todo")
    assert task.regression_required is True
    return tid


def _completed(conn, title="verified and closed") -> str:
    """A card driven through the whole chain to ``done``."""
    tid = _review_pending(conn, title=title)
    ok, detail = kb.record_verification(
        conn, tid, passed=True, verifier="reviewer", evidence={"exit_code": 0},
    )
    assert (ok, detail) == (True, kb.VERIFICATION_VERIFIED)
    assert kb.complete_task(conn, tid, summary="shipped") is True
    assert kb.get_task(conn, tid).status == "done"
    return tid


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

class TestDetection:
    def test_review_pending_is_detected(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _review_pending(conn)
            last = conn.execute(
                "SELECT id, kind, created_at FROM task_events "
                "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()
            _age(conn, tid, TIMEOUT + 60)

            found = _scan(conn)
            assert _ids(found) == [tid]
            entry = found[0]
            assert entry.status == "review"
            assert entry.verification_state == kb.VERIFICATION_PENDING
            assert entry.regression_required is False
            assert "verification_pending" in entry.reasons
            assert entry.age_seconds >= TIMEOUT
            # The clock is anchored to the last DURABLE lifecycle event, and
            # the alert names it so an operator can see what it was waiting on.
            assert entry.last_event_id == int(last["id"])
            assert entry.last_event_kind == last["kind"]

    def test_alert_is_auditable(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _review_pending(conn)
            _age(conn, tid, TIMEOUT + 60)
            _scan(conn)

            alerts = _events(conn, tid, kind=kb.GAUNTLET_STALE_EVENT)
            assert len(alerts) == 1
            payload = alerts[0][1]
            assert payload["status"] == "review"
            assert payload["verification_state"] == kb.VERIFICATION_PENDING
            assert payload["regression_required"] is False
            assert payload["age_seconds"] >= TIMEOUT
            assert payload["timeout_seconds"] == TIMEOUT
            assert payload["realert_seconds"] == REALERT
            assert payload["last_event_id"] is not None
            assert payload["last_event_at"] is not None
            assert payload["last_event_kind"]

    def test_failed_verdict_parked_in_review_is_detected(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _review_failed(conn)
            _age(conn, tid, TIMEOUT + 60)

            found = _scan(conn)
            assert _ids(found) == [tid]
            entry = found[0]
            assert entry.verification_state == kb.VERIFICATION_FAILED
            # A failing verdict arms the repair gate, so the debt is reported
            # alongside the failure — both are why this card is not finished.
            assert entry.regression_required is True
            assert entry.reasons == ["verification_failed", "regression_required"]

    def test_regression_debt_in_rework_is_detected(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _rework_owed(conn)
            status = kb.get_task(conn, tid).status
            _age(conn, tid, TIMEOUT + 60)

            found = _scan(conn)
            assert _ids(found) == [tid]
            entry = found[0]
            assert entry.regression_required is True
            # The verdict was spent when the repair leg started, so the head is
            # clear — the debt on the row is what keeps this unfinished.
            assert entry.verification_state is None
            assert entry.reasons == ["regression_required", f"parked_{status}"]

    def test_parked_ready_and_blocked_are_detected(self, kanban_home):
        with kb.connect_closing() as conn:
            ready = kb.create_task(
                conn, title="never picked up", assignee="default", gauntlet=True,
            )
            blocked = kb.create_task(
                conn, title="blocked forever", assignee="default", gauntlet=True,
            )
            assert kb.block_task(
                conn, blocked, reason="needs a decision", kind="needs_input",
            ) is True
            _age(conn, ready, TIMEOUT + 60)
            _age(conn, blocked, TIMEOUT + 60)

            found = {e.task_id: e for e in _scan(conn)}
            assert set(found) == {ready, blocked}
            assert found[ready].reasons == ["parked_ready"]
            assert found[blocked].reasons == ["parked_blocked"]

    def test_fresh_work_is_not_detected(self, kanban_home):
        with kb.connect_closing() as conn:
            _review_pending(conn)
            _rework_owed(conn)
            kb.create_task(
                conn, title="brand new", assignee="default", gauntlet=True,
            )
            assert _scan(conn) == []
            # Just short of the window is still fresh — the boundary is not
            # allowed to drift into "nearly stale counts".
            tid = _review_pending(conn, title="almost")
            _age(conn, tid, TIMEOUT - 60)
            assert _scan(conn) == []

    def test_done_and_archived_are_never_flagged(self, kanban_home):
        with kb.connect_closing() as conn:
            done = _completed(conn)
            archived = _completed(conn, title="verified, closed, filed")
            assert kb.archive_task(conn, archived) is True
            assert kb.get_task(conn, archived).status == "archived"
            _age(conn, done, TIMEOUT * 10)
            _age(conn, archived, TIMEOUT * 10)

            assert _scan(conn) == []
            assert _events(conn, done, kind=kb.GAUNTLET_STALE_EVENT) == []
            assert _events(conn, archived, kind=kb.GAUNTLET_STALE_EVENT) == []

    def test_running_work_is_left_to_the_heartbeat_path(self, kanban_home):
        """``running`` belongs to detect_stale_running, not to this scan.

        A wedged worker is reclaimed to ``ready`` there, and this scan picks
        the card up from ``ready``. Flagging it here as well would call every
        live, heartbeating worker abandoned.
        """
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="long job", assignee="default", gauntlet=True,
            )
            assert kb.claim_task(conn, tid).status == "running"
            _age(conn, tid, TIMEOUT * 3)
            assert _scan(conn) == []

    def test_non_enforced_task_is_not_flagged(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="classic lifecycle", assignee="default",
            )
            _age(conn, tid, TIMEOUT * 3)
            assert _scan(conn) == []

    def test_board_wide_enforcement_brings_a_task_into_scope(
        self, kanban_home, monkeypatch,
    ):
        """``kanban.gauntlet_enforcement`` raises the floor with no backfill."""
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="classic lifecycle", assignee="default",
            )
            _age(conn, tid, TIMEOUT * 3)
            assert _scan(conn) == []

            monkeypatch.setattr(kb, "gauntlet_enforcement_default", lambda: True)
            assert _ids(_scan(conn)) == [tid]


# ---------------------------------------------------------------------------
# Detection is not workflow mutation
# ---------------------------------------------------------------------------

class TestNoMutation:
    def test_scan_changes_no_task_state(self, kanban_home):
        with kb.connect_closing() as conn:
            parked = _review_pending(conn)
            owed = _rework_owed(conn)
            blocked = kb.create_task(
                conn, title="blocked", assignee="default", gauntlet=True,
            )
            kb.block_task(conn, blocked, reason="needs input", kind="needs_input")
            for tid in (parked, owed, blocked):
                _age(conn, tid, TIMEOUT + 60)

            before = {t: _snapshot(conn, t) for t in (parked, owed, blocked)}
            runs_before = conn.execute(
                "SELECT count(*) c FROM task_runs"
            ).fetchone()["c"]
            ledger_before = conn.execute(
                "SELECT count(*) c FROM task_verifications"
            ).fetchone()["c"]

            assert len(_scan(conn)) == 3

            for tid in (parked, owed, blocked):
                assert _snapshot(conn, tid) == before[tid], (
                    f"{tid} was mutated by a detection pass"
                )
            # No run was opened or closed, and no verdict was invented.
            assert conn.execute(
                "SELECT count(*) c FROM task_runs"
            ).fetchone()["c"] == runs_before
            assert conn.execute(
                "SELECT count(*) c FROM task_verifications"
            ).fetchone()["c"] == ledger_before

    def test_scan_writes_only_the_alert_event(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _review_pending(conn)
            _age(conn, tid, TIMEOUT + 60)
            before = [k for k, _ in _events(conn, tid)]

            _scan(conn)

            after = [k for k, _ in _events(conn, tid)]
            assert after == before + [kb.GAUNTLET_STALE_EVENT]


# ---------------------------------------------------------------------------
# One alert per episode
# ---------------------------------------------------------------------------

class TestEpisodeSuppression:
    def test_duplicate_scan_is_suppressed(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _review_pending(conn)
            _age(conn, tid, TIMEOUT + 60)

            assert _ids(_scan(conn)) == [tid]
            # Nothing has changed on the card, so re-scanning must not
            # re-alert — otherwise a 60-second dispatcher tick emits 60 events
            # an hour about one unchanged card.
            for _ in range(3):
                assert _scan(conn) == []
            assert len(_events(conn, tid, kind=kb.GAUNTLET_STALE_EVENT)) == 1

    def test_progress_ends_the_episode_and_arms_a_new_one(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _review_pending(conn)
            _age(conn, tid, TIMEOUT + 60)
            assert _ids(_scan(conn)) == [tid]

            # Durable progress: someone touched the card.
            kb.add_comment(conn, tid, "reviewer", "picking this up now")
            # The clock restarts from that event, so the card is fresh again.
            assert _scan(conn) == []
            assert len(_events(conn, tid, kind=kb.GAUNTLET_STALE_EVENT)) == 1

            # And if it is abandoned AGAIN, that is a new episode — it alerts
            # immediately on going stale, without waiting out the re-alert
            # window that governs an unchanged one.
            _age(conn, tid, TIMEOUT + 60)
            assert _ids(_scan(conn, realert_seconds=TIMEOUT * 100)) == [tid]
            assert len(_events(conn, tid, kind=kb.GAUNTLET_STALE_EVENT)) == 2

    def test_unchanged_episode_realerts_once_the_window_elapses(
        self, kanban_home,
    ):
        with kb.connect_closing() as conn:
            tid = _review_pending(conn)
            _age(conn, tid, TIMEOUT + 60)
            assert _ids(_scan(conn)) == [tid]
            assert _scan(conn) == []

            # Let the re-alert window pass with still nothing happening: the
            # card is still abandoned, and silence forever is its own failure.
            _age(conn, tid, REALERT + 60)
            assert _ids(_scan(conn)) == [tid]
            assert len(_events(conn, tid, kind=kb.GAUNTLET_STALE_EVENT)) == 2

    def test_zero_realert_means_alert_once_per_episode(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _review_pending(conn)
            _age(conn, tid, TIMEOUT + 60)
            assert _ids(_scan(conn, realert_seconds=0)) == [tid]

            _age(conn, tid, REALERT * 10)
            assert _scan(conn, realert_seconds=0) == []
            assert len(_events(conn, tid, kind=kb.GAUNTLET_STALE_EVENT)) == 1

            # Progress still starts a fresh episode, which can alert again.
            kb.add_comment(conn, tid, "reviewer", "looking")
            _age(conn, tid, TIMEOUT + 60)
            assert _ids(_scan(conn, realert_seconds=0)) == [tid]
            assert len(_events(conn, tid, kind=kb.GAUNTLET_STALE_EVENT)) == 2

    def test_scan_chatter_does_not_count_as_progress(self, kanban_home):
        """The alert must not reset the clock it reads.

        Nor may the per-tick dispatcher chatter that fires precisely while
        nothing is happening — a card the respawn guard skips every minute
        would otherwise look freshest exactly when it is most abandoned.
        """
        with kb.connect_closing() as conn:
            tid = _review_pending(conn)
            _age(conn, tid, TIMEOUT + 60)
            first = _scan(conn)[0]

            with kb.write_txn(conn):
                kb._append_event(conn, tid, "respawn_guarded", {"reason": "x"})
            conn.commit()

            _age(conn, tid, REALERT + 60)
            again = _scan(conn)
            assert _ids(again) == [tid]
            # Same episode: the marker is unchanged, so this is the re-alert
            # path rather than a "progress happened" reset.
            assert again[0].last_event_id == first.last_event_id


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class TestConfiguration:
    def test_defaults_are_conservative(self):
        kanban_defaults = DEFAULT_CONFIG["kanban"]
        assert kanban_defaults["gauntlet_stale_timeout_seconds"] == 14400
        assert kanban_defaults["gauntlet_stale_realert_seconds"] == 14400
        assert kb.DEFAULT_GAUNTLET_STALE_TIMEOUT_SECONDS == 14400
        assert kb.DEFAULT_GAUNTLET_STALE_REALERT_SECONDS == 14400

    def test_zero_timeout_disables_detection(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _review_pending(conn)
            _age(conn, tid, TIMEOUT * 100)

            assert kb.detect_stale_gauntlet_work(
                conn, stale_timeout_seconds=0,
            ) == []
            assert _events(conn, tid, kind=kb.GAUNTLET_STALE_EVENT) == []

    def test_config_zero_disables_detection(self, kanban_home, monkeypatch):
        """The config key, not just the argument, is a real off switch."""
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda *a, **k: {"kanban": {"gauntlet_stale_timeout_seconds": 0}},
        )
        assert kb.gauntlet_stale_timeout_default() == 0
        with kb.connect_closing() as conn:
            tid = _review_pending(conn)
            _age(conn, tid, TIMEOUT * 100)
            assert kb.detect_stale_gauntlet_work(conn) == []
            assert _events(conn, tid, kind=kb.GAUNTLET_STALE_EVENT) == []

    def test_config_values_are_read_live(self, kanban_home, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda *a, **k: {
                "kanban": {
                    "gauntlet_stale_timeout_seconds": 60,
                    "gauntlet_stale_realert_seconds": 30,
                }
            },
        )
        assert kb.gauntlet_stale_timeout_default() == 60
        assert kb.gauntlet_stale_realert_default() == 30
        with kb.connect_closing() as conn:
            tid = _review_pending(conn)
            _age(conn, tid, 120)
            assert _ids(kb.detect_stale_gauntlet_work(conn)) == [tid]

    def test_malformed_config_falls_back_to_the_default(
        self, kanban_home, monkeypatch,
    ):
        """A typo must not silently disable the scan — or crash a tick."""
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda *a, **k: {
                "kanban": {"gauntlet_stale_timeout_seconds": "four hours"}
            },
        )
        assert kb.gauntlet_stale_timeout_default() == (
            kb.DEFAULT_GAUNTLET_STALE_TIMEOUT_SECONDS
        )

        def _boom(*a, **k):
            raise RuntimeError("config unreadable")

        monkeypatch.setattr("hermes_cli.config.load_config", _boom)
        assert kb.gauntlet_stale_realert_default() == (
            kb.DEFAULT_GAUNTLET_STALE_REALERT_SECONDS
        )


# ---------------------------------------------------------------------------
# Surfaced through the existing dispatcher tick
# ---------------------------------------------------------------------------

class TestDispatchTick:
    def test_tick_returns_stale_ids_and_changes_no_state(
        self, kanban_home, monkeypatch,
    ):
        # The review lane would legitimately claim a review card; disable it so
        # this test observes the freshness scan and nothing else.
        monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: False)

        def _never_spawn(*a, **k):
            raise AssertionError("the freshness scan must not spawn workers")

        with kb.connect_closing() as conn:
            parked = _review_pending(conn)
            blocked = kb.create_task(
                conn, title="blocked forever", assignee="default", gauntlet=True,
            )
            kb.block_task(conn, blocked, reason="needs input", kind="needs_input")
            _age(conn, parked, TIMEOUT + 60)
            _age(conn, blocked, TIMEOUT + 60)
            before = {t: _snapshot(conn, t) for t in (parked, blocked)}

            result = kb.dispatch_once(
                conn,
                spawn_fn=_never_spawn,
                gauntlet_stale_timeout_seconds=TIMEOUT,
                gauntlet_stale_realert_seconds=REALERT,
            )

            assert sorted(result.gauntlet_stale) == sorted([parked, blocked])
            # Observability only: the tick reported them and moved nothing.
            for tid in (parked, blocked):
                assert _snapshot(conn, tid) == before[tid]
            assert result.spawned == []

    def test_tick_does_not_repeat_an_unchanged_episode(
        self, kanban_home, monkeypatch,
    ):
        monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: False)
        with kb.connect_closing() as conn:
            tid = _review_pending(conn)
            _age(conn, tid, TIMEOUT + 60)

            first = kb.dispatch_once(
                conn,
                spawn_fn=lambda *a, **k: None,
                gauntlet_stale_timeout_seconds=TIMEOUT,
                gauntlet_stale_realert_seconds=REALERT,
            )
            second = kb.dispatch_once(
                conn,
                spawn_fn=lambda *a, **k: None,
                gauntlet_stale_timeout_seconds=TIMEOUT,
                gauntlet_stale_realert_seconds=REALERT,
            )
            assert first.gauntlet_stale == [tid]
            assert second.gauntlet_stale == []
            assert len(_events(conn, tid, kind=kb.GAUNTLET_STALE_EVENT)) == 1

    def test_tick_stays_up_when_the_scan_fails(self, kanban_home, monkeypatch):
        """An observability pass must never take a dispatcher tick down."""
        def _boom(*a, **k):
            raise RuntimeError("scan exploded")

        monkeypatch.setattr(kb, "detect_stale_gauntlet_work", _boom)
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="work", assignee="default")
            result = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
            assert result.gauntlet_stale == []
            assert [t[0] for t in result.spawned] == [tid]

    def test_disabled_scan_leaves_the_tick_untouched(
        self, kanban_home, monkeypatch,
    ):
        monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: False)
        with kb.connect_closing() as conn:
            tid = _review_pending(conn)
            _age(conn, tid, TIMEOUT * 100)
            result = kb.dispatch_once(
                conn,
                spawn_fn=lambda *a, **k: None,
                gauntlet_stale_timeout_seconds=0,
            )
            assert result.gauntlet_stale == []
            assert _events(conn, tid, kind=kb.GAUNTLET_STALE_EVENT) == []


def test_scan_is_cheap_enough_to_run_every_tick(kanban_home):
    """Sanity bound: the scan is a per-tick pass, not a report job."""
    with kb.connect_closing() as conn:
        for i in range(40):
            tid = kb.create_task(
                conn, title=f"card {i}", assignee="default", gauntlet=True,
            )
            _age(conn, tid, TIMEOUT + 60)
        started = time.monotonic()
        found = _scan(conn)
        elapsed = time.monotonic() - started
    assert len(found) == 40
    assert elapsed < 5.0
