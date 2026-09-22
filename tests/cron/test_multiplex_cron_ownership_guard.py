"""Multi-profile cron ownership must fail closed on the isolation machinery.

Ticking several profiles' cron stores from one process makes that process
execute scheduled work on behalf of every one of those profiles. The only
thing that keeps a secondary profile's job from resolving the LAUNCH
profile's credentials is ``agent.secret_scope``'s multiplex flag: with it
set, an unscoped ``get_secret`` read raises; with it clear, the same read
silently returns whatever is in the launch process's ``os.environ``.

So multi-profile cron ownership must not be reachable unless that machinery
is explicitly enabled and the served profile set is valid. These tests pin
the gate in both directions — it refuses when isolation is not established,
and it does NOT stand in the way once it is — plus the invariant that the
single-profile (non-multiplex) path is untouched by any of it.
"""
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from cron.scheduler_provider import (
    InProcessCronScheduler,
    MultiplexCronIsolationError,
    multiplex_cron_isolation_error,
)


@pytest.fixture
def multiplex_flag():
    """Set/restore the process-global multiplex flag around one test."""
    from agent import secret_scope

    original = secret_scope.is_multiplex_active()

    def _set(active: bool):
        secret_scope.set_multiplex_active(active)

    try:
        yield _set
    finally:
        secret_scope.set_multiplex_active(original)


@pytest.fixture
def two_profiles(tmp_path):
    homes = []
    for name in ("default", "home-ops"):
        home = tmp_path / name
        (home / "cron").mkdir(parents=True)
        homes.append((name, home))
    return homes


def _wait_until(predicate, timeout=10.0, interval=0.005):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ── The gate itself ──────────────────────────────────────────────────────


def test_refuses_when_multiplexing_is_not_enabled(two_profiles, multiplex_flag):
    """The isolation flag being off is on its own disqualifying.

    This is the case that matters: the profile homes are perfectly valid, so
    nothing else in the request looks wrong. Without the flag, get_secret()
    falls back to os.environ for every one of these profiles' jobs.
    """
    multiplex_flag(False)

    reason = multiplex_cron_isolation_error(two_profiles)

    assert reason is not None
    assert "multiplexing is not enabled" in reason


def test_allows_when_enabled_and_the_profile_set_is_valid(two_profiles, multiplex_flag):
    """Positive control — the gate is an enablement check, not a blanket ban."""
    multiplex_flag(True)

    assert multiplex_cron_isolation_error(two_profiles) is None


def test_refuses_an_empty_profile_set(multiplex_flag):
    multiplex_flag(True)

    reason = multiplex_cron_isolation_error([])

    assert reason is not None
    assert "no served profile homes" in reason


def test_refuses_a_home_that_does_not_exist(tmp_path, multiplex_flag):
    """An unresolvable home cannot be proven isolated, so it is refused."""
    multiplex_flag(True)
    good = tmp_path / "default"
    (good / "cron").mkdir(parents=True)

    reason = multiplex_cron_isolation_error(
        [("default", good), ("ghost", tmp_path / "never-created")]
    )

    assert reason is not None
    assert "not an existing directory" in reason


def test_refuses_two_profiles_that_resolve_to_one_store(tmp_path, multiplex_flag):
    """Two names over one store is shared ownership, not per-profile ownership."""
    multiplex_flag(True)
    home = tmp_path / "default"
    (home / "cron").mkdir(parents=True)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(home, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("symlinks unavailable on this host")

    reason = multiplex_cron_isolation_error([("default", home), ("alias", alias)])

    assert reason is not None
    assert "resolve to the same store" in reason


def test_a_bare_home_entry_is_accepted_like_a_named_one(tmp_path, multiplex_flag):
    """``profile_homes`` accepts bare paths as well as (name, home) tuples."""
    multiplex_flag(True)
    a = tmp_path / "a"
    b = tmp_path / "b"
    for d in (a, b):
        (d / "cron").mkdir(parents=True)

    assert multiplex_cron_isolation_error([a, b]) is None


# ── The gate is enforced where ownership is actually taken ───────────────


def test_start_multiplex_cannot_be_called_past_the_gate(two_profiles, multiplex_flag):
    """A direct call to the multiplex ticker raises rather than taking over."""
    multiplex_flag(False)
    prov = InProcessCronScheduler()

    with pytest.raises(MultiplexCronIsolationError) as exc:
        prov._start_multiplex(threading.Event(), profile_homes=two_profiles)

    assert "multi-profile cron ownership refused" in str(exc.value)


def test_start_falls_back_to_the_single_profile_ticker_when_isolation_is_absent(
    two_profiles, multiplex_flag
):
    """No profile store is ticked under multiplex when the flag is off.

    ``use_cron_store`` is the seam that points the ticker at a *specific
    profile's* cron store. If it is never entered, no secondary profile's
    jobs ran — which is the fail-closed outcome. The ticker itself keeps
    running on the launch profile, so cron does not go dark.
    """
    multiplex_flag(False)
    stop = threading.Event()
    prov = InProcessCronScheduler()
    ticks: list[int] = []
    scoped_homes: list[Path] = []

    def _tracking_tick(*args, **kwargs):
        ticks.append(1)
        return 0

    def _tracking_store(home):
        scoped_homes.append(home)
        raise AssertionError(
            "a profile cron store was scoped despite refused multiplex ownership"
        )

    with patch("cron.scheduler.tick", side_effect=_tracking_tick), \
         patch("cron.jobs.use_cron_store", side_effect=_tracking_store), \
         patch("cron.jobs.record_ticker_heartbeat", lambda **kw: None), \
         patch("cron.jobs.clear_ticker_error", lambda: None), \
         patch.object(InProcessCronScheduler, "recover_interrupted", return_value=0):
        t = threading.Thread(
            target=prov.start,
            args=(stop,),
            kwargs={"interval": 0, "profile_homes": two_profiles},
            daemon=True,
        )
        t.start()
        assert _wait_until(lambda: len(ticks) >= 2), "the launch ticker never ran"
        stop.set()
        t.join(timeout=5)

    assert not t.is_alive()
    assert scoped_homes == [], "multiplex ownership was taken without isolation"


def test_start_takes_multiplex_ownership_once_isolation_is_enabled(
    two_profiles, multiplex_flag
):
    """Positive control on the same seam: with the flag set, each profile ticks."""
    multiplex_flag(True)
    stop = threading.Event()
    prov = InProcessCronScheduler()
    scoped_homes: list[str] = []

    class _NullStore:
        def __init__(self, home):
            scoped_homes.append(str(home))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    with patch("cron.scheduler.tick", return_value=0), \
         patch("cron.jobs.use_cron_store", _NullStore), \
         patch("cron.jobs.record_ticker_heartbeat", lambda **kw: None), \
         patch("cron.jobs.clear_ticker_error", lambda: None), \
         patch.object(InProcessCronScheduler, "recover_interrupted", return_value=0):
        t = threading.Thread(
            target=prov.start,
            args=(stop,),
            kwargs={"interval": 0, "profile_homes": two_profiles},
            daemon=True,
        )
        t.start()
        expected = {str(home) for _name, home in two_profiles}
        assert _wait_until(lambda: expected.issubset(set(scoped_homes))), (
            f"expected every profile store to be scoped, saw {scoped_homes}"
        )
        stop.set()
        t.join(timeout=5)

    assert not t.is_alive()


def test_single_profile_ticker_is_untouched_by_the_gate(multiplex_flag):
    """No ``profile_homes`` → the legacy path runs and never consults the gate."""
    multiplex_flag(False)
    stop = threading.Event()
    prov = InProcessCronScheduler()
    ticks: list[int] = []

    def _tracking_tick(*args, **kwargs):
        ticks.append(1)
        return 0

    with patch("cron.scheduler.tick", side_effect=_tracking_tick), \
         patch("cron.jobs.use_cron_store", side_effect=AssertionError), \
         patch("cron.jobs.record_ticker_heartbeat", lambda **kw: None), \
         patch("cron.jobs.clear_ticker_error", lambda: None), \
         patch.object(InProcessCronScheduler, "recover_interrupted", return_value=0):
        t = threading.Thread(
            target=prov.start,
            args=(stop,),
            kwargs={"interval": 0},
            daemon=True,
        )
        t.start()
        assert _wait_until(lambda: len(ticks) >= 2)
        stop.set()
        t.join(timeout=5)

    assert not t.is_alive()
