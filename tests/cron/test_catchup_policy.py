"""The local missed-run policy preserves grace (cron.catch_up_missed)."""
from datetime import timedelta

import pytest

from cron import jobs


@pytest.mark.parametrize("catch_up", [True, False])
def test_missed_policy_preserves_grace(tmp_path, monkeypatch, catch_up):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("".join(["cron:", chr(10), f"  catch_up_missed: {str(catch_up).lower()}", chr(10)]), encoding="utf-8")
    with jobs.use_cron_store(tmp_path / "cron"):
        now = jobs._hermes_now()
        for name, lag in [("stale", 14400), ("grace", 30)]:
            job = jobs.create_job(prompt=name, schedule="every 1h", model="fixture", deliver="local")
            stored = jobs.load_jobs()
            row = next(row for row in stored if row["id"] == job["id"])
            row["next_run_at"] = (now - timedelta(seconds=lag)).isoformat()
            jobs.save_jobs(stored)
        due = {job["prompt"] for job in jobs.get_due_jobs()}
        assert due == ({"stale", "grace"} if catch_up else {"grace"})
        stale = next(row for row in jobs.load_jobs() if row["prompt"] == "stale")
        assert jobs._ensure_aware(jobs.datetime.fromisoformat(stale["next_run_at"])) > now


@pytest.mark.parametrize("uncomputable", [False, True])
def test_default_and_uncomputable_still_catch_up(tmp_path, monkeypatch, uncomputable):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    if uncomputable:
        (tmp_path / "config.yaml").write_text("".join(["cron:", chr(10), "  catch_up_missed: false", chr(10)]), encoding="utf-8")
    with jobs.use_cron_store(tmp_path / "cron"):
        job = jobs.create_job(prompt="default", schedule="every 1h", model="fixture", deliver="local")
        stored = jobs.load_jobs()
        stored[0]["next_run_at"] = (jobs._hermes_now() - timedelta(hours=4)).isoformat()
        jobs.save_jobs(stored)
        if uncomputable:
            monkeypatch.setattr(jobs, "compute_next_run", lambda *args: None)
        assert [row["id"] for row in jobs.get_due_jobs()] == [job["id"]]
