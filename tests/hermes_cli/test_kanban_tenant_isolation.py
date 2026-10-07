"""Company isolation at claim and dispatch — Governance Phase 3, rule 3.

Doctrine basis: execution contract rule 3 ("One task. One company — or
SHARED/GOVERNANCE when doctrine explicitly authorizes it. Uncertain company
scope means FORBIDDEN."), sovereignty.md, data-isolation.md.

Before this gate ``tasks.tenant`` scoped only lesson injection; no claim or
dispatch path refused a profile from company A executing a task tagged to
company B (Phase 3 discovery t_5d7052f9, verified FAIL by t_85eaad92). These
tests pin the gate at every door it now guards: create, assign, claim, review
claim, and both dispatcher loops.

The profile -> company mapping is patched in-memory here; on a real install it
comes from each profile's ``profile.yaml`` (``company:``), which the last class
covers against a real temp profiles root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import profiles

# Deliberately NOT ``all_assignees_spawnable``: that fixture also maps every
# synthetic assignee to the shared lane, which is exactly what this module
# must not assume. The ``lanes`` fixture below installs both patches itself.


LANES = {
    "orion_lead": "orion",
    "orion_worker": "orion",
    "glass_pepper_lead": "glass-pepper",
    "erika": profiles.SHARED_LANE,
    # "untagged_worker" deliberately absent: no company recorded.
}


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Pin the board root to the temp home and drop every kanban pin a
    # dispatched worker inherits. HERMES_KANBAN_DB outranks HERMES_HOME in
    # kanban_db_path(); without this, running the module from a worker
    # outside the repo's conftest sandbox writes fixture cards to the
    # operator's live board (this happened: t_92521910 run 3496 created
    # t_3f7e43ae, 2026-10-07).
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture(autouse=True)
def lanes(monkeypatch):
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    monkeypatch.setattr(
        profiles, "profile_company",
        lambda name: profiles.SHARED_LANE if name == "default" else LANES.get(name),
    )


def _events(conn, tid, kind):
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


# ---------------------------------------------------------------------------
# The predicate itself
# ---------------------------------------------------------------------------

class TestPredicate:
    @pytest.mark.parametrize("tenant", [None, "", "   "])
    def test_untagged_task_is_open_to_any_profile(self, tenant):
        for who in ("orion_lead", "glass_pepper_lead", "untagged_worker", "erika", "default"):
            assert kb.tenant_claim_conflict(tenant, who) is None

    def test_same_company_allowed_case_insensitively(self):
        assert kb.tenant_claim_conflict("Orion", "orion_lead") is None
        assert kb.tenant_claim_conflict("orion ", "orion_worker") is None

    def test_other_company_refused(self):
        c = kb.tenant_claim_conflict("orion", "glass_pepper_lead")
        assert c is not None and c["reason"] == "tenant_mismatch"
        assert c["task_tenant"] == "orion" and c["profile_company"] == "glass-pepper"

    def test_untagged_profile_refused_on_tagged_task(self):
        c = kb.tenant_claim_conflict("orion", "untagged_worker")
        assert c is not None and c["reason"] == "tenant_unknown_profile"
        assert c["profile_company"] is None

    def test_shared_profile_allowed_everywhere(self):
        assert kb.tenant_claim_conflict("orion", "erika") is None
        assert kb.tenant_claim_conflict(kb.TENANT_SHARED, "erika") is None
        assert kb.tenant_claim_conflict("orion", "default") is None
        # Legacy direct-executor shorthands ride the shared carrier.
        assert kb.tenant_claim_conflict("orion", "claude") is None
        assert kb.tenant_claim_conflict("orion", "atlas") is None

    def test_company_profile_refused_on_shared_task(self):
        c = kb.tenant_claim_conflict(kb.TENANT_SHARED, "orion_lead")
        assert c is not None and c["reason"] == "shared_task_company_profile"


# ---------------------------------------------------------------------------
# Write-time doors: create_task / assign_task
# ---------------------------------------------------------------------------

class TestAssignmentDoors:
    def test_create_refuses_cross_company_assignee(self, kanban_home):
        with kb.connect_closing() as conn:
            with pytest.raises(ValueError, match="tenant_mismatch"):
                kb.create_task(conn, title="x", assignee="glass_pepper_lead", tenant="orion")
            assert kb.list_tasks(conn) == []

    def test_create_refuses_untagged_assignee_on_tagged_task(self, kanban_home):
        with kb.connect_closing() as conn:
            with pytest.raises(ValueError, match="tenant_unknown_profile"):
                kb.create_task(conn, title="x", assignee="untagged_worker", tenant="orion")

    def test_create_allows_in_lane_and_shared(self, kanban_home):
        with kb.connect_closing() as conn:
            assert kb.create_task(conn, title="a", assignee="orion_lead", tenant="orion")
            assert kb.create_task(conn, title="b", assignee="erika", tenant="orion")
            assert kb.create_task(conn, title="c", assignee="default", tenant="orion")
            assert kb.create_task(conn, title="d", assignee="glass_pepper_lead")  # untagged card

    def test_assign_refuses_cross_company(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="x", assignee="orion_lead", tenant="orion")
            with pytest.raises(ValueError, match="may not execute a task in lane 'orion'"):
                kb.assign_task(conn, tid, "glass_pepper_lead")
            assert kb.get_task(conn, tid).assignee == "orion_lead"
            assert kb.assign_task(conn, tid, "erika") is True


# ---------------------------------------------------------------------------
# Claim doors: claim_task / claim_review_task
# ---------------------------------------------------------------------------

def _tagged_ready(conn, *, assignee, tenant="orion"):
    """A ready card whose lane disagrees with its assignee, created past the
    write-time door the way a legacy row or manual SQL would."""
    tid = kb.create_task(conn, title="lane test", assignee=assignee)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET tenant = ? WHERE id = ?", (tenant, tid))
    return tid


class TestClaimDoors:
    def test_claim_refused_and_recorded(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _tagged_ready(conn, assignee="glass_pepper_lead")
            assert kb.claim_task(conn, tid) is None
            task = kb.get_task(conn, tid)
            assert task.status == "ready" and task.claim_lock is None
            refusals = _events(conn, tid, "execution_refused")
            assert len(refusals) == 1
            assert refusals[0].payload["reason"] == "tenant_mismatch"
            assert refusals[0].payload["source"] == "claim_task"

    def test_claim_refused_for_untagged_profile(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _tagged_ready(conn, assignee="untagged_worker")
            assert kb.claim_task(conn, tid) is None
            assert _events(conn, tid, "execution_refused")[0].payload["reason"] == "tenant_unknown_profile"

    def test_claim_allowed_in_lane_and_shared(self, kanban_home):
        with kb.connect_closing() as conn:
            a = _tagged_ready(conn, assignee="orion_worker")
            b = _tagged_ready(conn, assignee="erika")
            assert kb.claim_task(conn, a) is not None
            assert kb.claim_task(conn, b) is not None

    def test_review_claim_refused_for_cross_company_reviewer(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="x", assignee="orion_lead", tenant="orion")
            run = kb.claim_task(conn, tid)
            assert run is not None
            assert kb.request_review(
                conn, tid, summary="done", reviewer="erika",
                expected_run_id=run.current_run_id,
            )
            # Reviewer drifts out of lane past the assignment door.
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET assignee = 'glass_pepper_lead' WHERE id = ?", (tid,))
            assert kb.claim_review_task(conn, tid) is None
            assert kb.get_task(conn, tid).status == "review"
            refusal = _events(conn, tid, "execution_refused")[-1]
            assert refusal.payload["reason"] == "tenant_mismatch"
            assert refusal.payload["source"] == "claim_review_task"

    def test_review_claim_allowed_for_shared_reviewer(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="x", assignee="orion_lead", tenant="orion")
            run = kb.claim_task(conn, tid)
            assert run is not None
            assert kb.request_review(
                conn, tid, summary="done", reviewer="erika",
                expected_run_id=run.current_run_id,
            )
            assert kb.claim_review_task(conn, tid) is not None


# ---------------------------------------------------------------------------
# Dispatcher doors: ready loop and review loop
# ---------------------------------------------------------------------------

class TestDispatcher:
    def test_ready_loop_skips_conflict_and_records_once(self, kanban_home):
        with kb.connect_closing() as conn:
            bad = _tagged_ready(conn, assignee="glass_pepper_lead")
            good = _tagged_ready(conn, assignee="orion_worker")
            spawned = []

            def spawn(task, workspace, board=None):
                spawned.append(task.id)
                return None

            r1 = kb.dispatch_once(conn, spawn_fn=spawn)
            assert spawned == [good]
            assert (bad, "glass_pepper_lead", "tenant_mismatch") in r1.skipped_tenant_conflict
            assert kb.get_task(conn, bad).status == "ready"
            assert len(_events(conn, bad, "claim_refused_tenant_conflict")) == 1
            # A second tick does not duplicate the record.
            kb.dispatch_once(conn, spawn_fn=spawn)
            assert len(_events(conn, bad, "claim_refused_tenant_conflict")) == 1
            # A different conflict (reassigned to another wrong lane) is a new record.
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET assignee = 'untagged_worker' WHERE id = ?", (bad,))
            kb.dispatch_once(conn, spawn_fn=spawn)
            kinds = [e.payload["reason"] for e in _events(conn, bad, "claim_refused_tenant_conflict")]
            assert kinds == ["tenant_mismatch", "tenant_unknown_profile"]
            assert spawned == [good]

    def test_dry_run_reports_but_writes_nothing(self, kanban_home):
        with kb.connect_closing() as conn:
            bad = _tagged_ready(conn, assignee="glass_pepper_lead")
            r = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, dry_run=True)
            assert r.skipped_tenant_conflict[0][0] == bad
            assert _events(conn, bad, "claim_refused_tenant_conflict") == []

    def test_review_loop_skips_conflict(self, kanban_home, monkeypatch):
        monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="x", assignee="orion_lead", tenant="orion")
            run = kb.claim_task(conn, tid)
            assert run is not None
            assert kb.request_review(
                conn, tid, summary="done", reviewer="erika",
                expected_run_id=run.current_run_id,
            )
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET assignee = 'glass_pepper_lead' WHERE id = ?", (tid,))
            spawned = []
            r = kb.dispatch_once(conn, spawn_fn=lambda t, w, board=None: spawned.append(t.id))
            assert spawned == []
            assert (tid, "glass_pepper_lead", "tenant_mismatch") in r.skipped_tenant_conflict
            assert kb.get_task(conn, tid).status == "review"


# ---------------------------------------------------------------------------
# The real mapping: profile.yaml ``company:``
# ---------------------------------------------------------------------------

class TestProfileCompanyFromDisk:
    @pytest.fixture
    def profile_env(self, tmp_path, monkeypatch):
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        return home

    def test_company_round_trips_and_normalizes(self, profile_env):
        pdir = profiles._get_profiles_root() / "orion_formation_services_lead"
        pdir.mkdir(parents=True)
        profiles.write_profile_meta(pdir, description="Olivia", company="  Orion ")
        assert profiles.read_profile_meta(pdir)["company"] == "orion"
        assert _real_profile_company("orion_formation_services_lead") == "orion"
        # Clearing leaves the profile untagged, not shared.
        profiles.write_profile_meta(pdir, company="")
        assert profiles.read_profile_meta(pdir)["company"] == ""
        assert _real_profile_company("orion_formation_services_lead") is None

    def test_default_is_shared_and_missing_profile_is_untagged(self, profile_env):
        assert _real_profile_company("default") == profiles.SHARED_LANE
        assert _real_profile_company("nobody_here") is None


def test_tenant_refusal_is_not_lifecycle_progress():
    """A refused claim recurs for as long as nobody fixes the routing; the
    stale scan must not read it as the card being worked."""
    assert "claim_refused_tenant_conflict" in kb._GAUNTLET_STALE_NONPROGRESS_EVENT_KINDS
    assert "execution_refused" in kb._GAUNTLET_STALE_NONPROGRESS_EVENT_KINDS


# ``profile_company`` is patched for the rest of the module; keep a handle on
# the real implementation for the disk-backed tests above.
_real_profile_company = profiles.profile_company
