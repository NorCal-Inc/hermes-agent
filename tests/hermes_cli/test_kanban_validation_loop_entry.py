from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'; home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    for var in ('HERMES_KANBAN_TASK','HERMES_KANBAN_RUN_ID','HERMES_EXECUTION_ID','HERMES_SESSION_ID','HERMES_EXECUTOR_LANE','HERMES_PROFILE','HERMES_PROFILE_NAME',kb.ENV_ACTOR_KIND,kb.ENV_ACTOR_ID):
        monkeypatch.delenv(var, raising=False)
    kb.init_db(); return home

def _human():
    return kb.ActorProvenance(kind=kb.ACTOR_KIND_HUMAN_INTERACTIVE, actor_id='christopher', cause=kb.CREATION_CAUSE_MANUAL_RELAY)

def _repair_prov():
    return kb.ActorProvenance(kind=kb.ACTOR_KIND_GOVERNED_AUTOMATION, actor_id='recovery', cause=kb.CREATION_CAUSE_RECOVERY)

def _repair(conn):
    subject = kb.create_task(conn, title='ordinary subject', assignee='default', provenance=_human())
    umbrella = kb.create_task(conn, title='ordinary umbrella', assignee='default', provenance=_human())
    return kb.create_repair_task(conn, title='repair runtime path', subject_id=subject, owner='erika', umbrella_id=umbrella, assignee='default', provenance=_repair_prov(), gauntlet=False)

def test_repair_is_loop_armed_and_claimable(kanban_home):
    with kb.connect_closing() as conn:
        tid = _repair(conn)
        row = conn.execute('select gauntlet_enforced,status from tasks where id=?',(tid,)).fetchone()
        assert row['gauntlet_enforced'] == 1
        timer = conn.execute('select state from observation_timers where task_id=? and kind=?',(tid,kb.VALIDATION_LOOP_TIMER_KIND)).fetchone()
        assert timer['state'] == kb.OBSERVATION_STATE_OBSERVING
        assert kb.claim_task(conn, tid, claimer='test') is not None

def test_repair_without_loop_state_cannot_claim_or_promote(kanban_home):
    with kb.connect_closing() as conn:
        tid = _repair(conn)
        timer_id = conn.execute('select id from observation_timers where task_id=?',(tid,)).fetchone()['id']
        assert kb.close_observation_timer(conn, timer_id, reason='adversarial missing-loop test') is True
        conn.execute("update tasks set status='blocked' where id=?",(tid,))
        assert kb.validation_loop_entry_valid(conn, tid)[0] is False
        assert kb.claim_task(conn, tid, claimer='test') is None
        ok, reason = kb.promote_task(conn, tid, actor='test')
        assert ok is False
        assert 'validation-loop admission refused' in reason
        events = [r['kind'] for r in conn.execute("select kind from task_events where task_id=?",(tid,))]
        assert 'execution_refused' in events

def test_direct_recovery_creation_cannot_opt_out(kanban_home):
    with kb.connect_closing() as conn:
        subject = kb.create_task(conn, title='subject', assignee='default', provenance=_human())
        umbrella = kb.create_task(conn, title='umbrella', assignee='default', provenance=_human())
        tid = kb.create_task(conn, title='direct recovery', assignee='default', provenance=_repair_prov(), recovery_owner='erika', repairs_task_id=subject, umbrella_task_id=umbrella, gauntlet=False)
        assert conn.execute('select gauntlet_enforced from tasks where id=?',(tid,)).fetchone()['gauntlet_enforced'] == 1
        assert conn.execute('select 1 from observation_timers where task_id=? and kind=?',(tid,kb.VALIDATION_LOOP_TIMER_KIND)).fetchone() is not None


def _close_loop(conn, tid):
    timer_id = conn.execute(
        'select id from observation_timers where task_id=? and kind=?',
        (tid, kb.VALIDATION_LOOP_TIMER_KIND),
    ).fetchone()['id']
    assert kb.close_observation_timer(conn, timer_id, reason='adversarial missing-loop test') is True


def test_all_administrative_admission_paths_fail_closed_without_loop(kanban_home):
    """The t_318800fe unlink->dispatch shape cannot bypass the loop."""
    with kb.connect_closing() as conn:
        tid = _repair(conn)
        parent = kb.create_task(conn, title='dependency parent', assignee='default', provenance=_human())
        kb.link_tasks(conn, parent, tid)
        # Reproduce the historical administrative sequence: unlink, promote,
        # then attempt dispatch. The dependency edge is not the loop state.
        assert kb.unlink_tasks(conn, parent, tid) is True
        _close_loop(conn, tid)
        conn.execute("update tasks set status='blocked' where id=?", (tid,))
        ok, reason = kb.promote_task(conn, tid, actor='operator')
        assert ok is False and 'validation-loop admission refused' in reason
        assert kb.unblock_task(conn, tid) is False
        assert kb.claim_task(conn, tid, claimer='dispatcher') is None


def test_review_claim_reassign_and_reclaim_paths_fail_closed_without_loop(kanban_home):
    with kb.connect_closing() as conn:
        tid = _repair(conn)
        _close_loop(conn, tid)
        conn.execute("update tasks set status='review' where id=?", (tid,))
        assert kb.claim_review_task(conn, tid, claimer='reviewer') is None
        # Explicit reassignment is an administrative recovery path too.  It
        # may change ownership, but it must not manufacture validation-loop
        # state or permit the reassigned repair to execute.
        assert kb.assign_task(conn, tid, 'default') is True
        assert kb.claim_task(conn, tid, claimer='dispatcher') is None
        conn.execute("update tasks set status='running', claim_lock='test-lock' where id=?", (tid,))
        assert kb.reclaim_task(conn, tid, reason='adversarial reclaim') is True
        # Reclaim may reset the phase, but it must not create loop state.
        assert kb.claim_task(conn, tid, claimer='dispatcher') is None


def test_terminal_completion_still_requires_independent_pass(kanban_home):
    with kb.connect_closing() as conn:
        tid = _repair(conn)
        with pytest.raises(kb.VerificationRequiredError):
            kb.complete_task(conn, tid, summary='unverified completion')
        row = conn.execute('select status, verification_state from tasks where id=?', (tid,)).fetchone()
        assert row['status'] != 'done'
        assert row['verification_state'] != kb.VERIFICATION_VERIFIED
