"""C1.3: kanban spawn wiring for the worker sandbox."""
import os
import shutil
import signal
import subprocess
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kdb
from hermes_cli import worker_sandbox as ws

CMD = ["hermes", "-p", "orion_lead", "chat", "-q", "x"]


def _task(executor_lane=None):
    return SimpleNamespace(id="t_test", executor_lane=executor_lane)


def _setup(monkeypatch, mode="enforce", lanes=("orion",), lane="orion",
           manifest=None, wrap=None):
    monkeypatch.setattr(ws, "sandbox_config", lambda cfg=None: {"mode": mode, "lanes": list(lanes)})
    monkeypatch.setattr(ws, "lane_for", lambda profile: lane)
    calls = {"load": 0, "wrap": 0}

    def _load(path=None):
        calls["load"] += 1
        if isinstance(manifest, Exception):
            raise manifest
        return manifest

    def _wrap(cmd, **kw):
        calls["wrap"] += 1
        if isinstance(wrap, Exception):
            raise wrap
        return wrap(cmd) if wrap else ["bwrap", "--die-with-parent", "--"] + list(cmd)

    monkeypatch.setattr(ws, "load_manifest", _load)
    monkeypatch.setattr(ws, "wrap", _wrap)
    return calls


def _apply(task, env):
    return kdb._apply_worker_sandbox(
        task, list(CMD), profile="orion_lead", profile_home="/tmp/ph",
        workspace="/tmp/ws", env=env,
    )


def test_mode_off_unchanged(monkeypatch):
    calls = _setup(monkeypatch, mode="off", manifest={"lanes": {}})
    env = {}
    assert _apply(_task(), env) == CMD
    assert env == {} and calls == {"load": 0, "wrap": 0}


def test_enforced_lane_wrapped(monkeypatch):
    _setup(monkeypatch, manifest={"lanes": {"orion": {}}})
    env = {}
    out = _apply(_task(), env)
    assert out[0] == "bwrap" and out[-len(CMD):] == CMD
    assert env[ws.SANDBOX_NO_REFRESH_ENV] == "1"


def test_executor_lane_unsandboxed(monkeypatch):
    calls = _setup(monkeypatch, manifest={"lanes": {"orion": {}}})
    env = {}
    assert _apply(_task(executor_lane="claude"), env) == CMD
    assert env == {} and calls["wrap"] == 0


def test_enforced_broken_manifest_refused(monkeypatch):
    _setup(monkeypatch, manifest=ws.SandboxConfigError("bad yaml"))
    env = {}
    with pytest.raises(RuntimeError, match="refusing to spawn enforced lane 'orion'"):
        _apply(_task(), env)
    assert ws.SANDBOX_NO_REFRESH_ENV not in env


def test_enforced_missing_manifest_refused(monkeypatch):
    _setup(monkeypatch, manifest=None)
    with pytest.raises(RuntimeError, match="missing"):
        _apply(_task(), {})


def test_enforced_wrap_error_refused(monkeypatch):
    _setup(monkeypatch, manifest={"lanes": {}}, wrap=ws.SandboxConfigError("forbidden path"))
    with pytest.raises(RuntimeError, match="forbidden path"):
        _apply(_task(), {})


def test_enforced_wrap_noop_refused(monkeypatch):
    _setup(monkeypatch, manifest={"lanes": {}}, wrap=lambda cmd: list(cmd))
    with pytest.raises(RuntimeError, match="not applied"):
        _apply(_task(), {})


def test_non_enforced_lane_broken_manifest_spawns(monkeypatch):
    calls = _setup(monkeypatch, lanes=("logos",), manifest=ws.SandboxConfigError("bad"))
    assert _apply(_task(), {}) == CMD
    assert calls == {"load": 0, "wrap": 0}


def test_shared_profile_unsandboxed(monkeypatch):
    _setup(monkeypatch, lane=None, manifest=ws.SandboxConfigError("bad"))
    assert _apply(_task(), {}) == CMD


def test_invalid_mode_refused(monkeypatch):
    def _bad(cfg=None):
        raise ws.SandboxConfigError("mode must be off|enforce")
    monkeypatch.setattr(ws, "sandbox_config", _bad)
    with pytest.raises(RuntimeError, match="invalid"):
        _apply(_task(), {})


def test_dispatcher_records_refusal_as_spawn_failure():
    # The dispatcher wraps spawn_fn in `except Exception` -> _record_spawn_failure,
    # so a refusal is recorded with its message and no worker pid is set.
    import inspect
    src = inspect.getsource(kdb)
    assert "_record_spawn_failure(\n                conn, claimed.id, str(exc)," in src


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap absent")
def test_sigterm_outer_bwrap_kills_inner_worker():
    marker = "317.123"
    proc = subprocess.Popen(
        ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
         "--unshare-pid", "--die-with-parent", "--new-session", "--",
         "sleep", marker],
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        deadline = time.time() + 5
        while time.time() < deadline and subprocess.run(
            ["pgrep", "-f", f"^sleep {marker}$"], capture_output=True
        ).returncode != 0:
            time.sleep(0.1)
        assert subprocess.run(["pgrep", "-f", f"^sleep {marker}$"],
                              capture_output=True).returncode == 0, "inner never started"
        os.kill(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
        deadline = time.time() + 5
        while time.time() < deadline:
            if subprocess.run(["pgrep", "-f", f"^sleep {marker}$"],
                              capture_output=True).returncode != 0:
                break
            time.sleep(0.1)
        assert subprocess.run(["pgrep", "-f", f"^sleep {marker}$"],
                              capture_output=True).returncode != 0, "inner survived"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_enforced_lane_gets_openrouter_key_in_env_not_argv(monkeypatch):
    _setup(monkeypatch, manifest={"lanes": {"orion": {}}})
    monkeypatch.setattr(kdb, "_shared_openrouter_key", lambda: "FAKE-OR-KEY")
    env = {}
    out = _apply(_task(), env)
    assert env["OPENROUTER_API_KEY"] == "FAKE-OR-KEY"
    assert not any("FAKE-OR-KEY" in a for a in out)


def test_unsandboxed_lane_env_untouched(monkeypatch):
    _setup(monkeypatch, mode="off", manifest={"lanes": {}})
    monkeypatch.setattr(kdb, "_shared_openrouter_key", lambda: "FAKE-OR-KEY")
    env = {}
    _apply(_task(), env)
    assert "OPENROUTER_API_KEY" not in env
