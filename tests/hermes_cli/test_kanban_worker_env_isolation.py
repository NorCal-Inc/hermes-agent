"""A Kanban worker for another profile must not inherit the dispatcher's env.

``_default_spawn`` used to build the worker environment as a bare
``env = dict(os.environ)`` and hand it straight to ``subprocess.Popen``. The
only removals were ``gateway.session_context`` routing keys and ``HERMES_TUI``;
``HERMES_HOME`` was then repointed at the assignee's profile. There was no
credential scrub on that path at all.

The dispatcher runs inside the gateway (``kanban.dispatch_in_gateway``), so
that environment is the launch gateway's own — its provider keys, its bot
tokens, whatever systemd injects — and every lane's worker was built from one
identical environment differing only in ``HERMES_HOME``.

The authority test for scrubbing is "does this worker act for a ROUTED home",
not the gateway-wide ``is_multiplex_active()`` flag: gating on the flag leaves
every single-profile host — the common Kanban deployment — unprotected.

Upstream: ``bc0a42fd96``. NorCal card ``t_ab4016a8`` (Finding A of
``t_82336f3d``).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# Credentials the dispatcher plausibly carries. Each is removed by a different
# arm of the scrub, so a partial regression is still caught:
#   * provider keys / bot tokens / GitHub auth -> _HERMES_PROVIDER_ENV_BLOCKLIST
#   * AUXILIARY_<TASK>_API_KEY                 -> _is_hermes_internal_secret
#   * GATEWAY_RELAY_SECRET                     -> _is_hermes_internal_secret
DISPATCHER_CREDENTIAL_ENV = {
    "OPENAI_API_KEY": "launch-openai",
    "ANTHROPIC_API_KEY": "launch-anthropic",
    "TELEGRAM_BOT_TOKEN": "launch-telegram",
    "GH_TOKEN": "launch-github",
    "AUXILIARY_VISION_API_KEY": "launch-auxiliary",
    "GATEWAY_RELAY_SECRET": "launch-relay",
}

# Operator-defined company credentials. The registry-derived scrub list knows
# NOTHING about these names, which is why the launch-``.env`` residue strip
# exists: on the live host 74 of the launch profile's 93 env keys survive the
# blocklist scrub, 27 of them credential-shaped.
LAUNCH_DOTENV_COMPANY_ENV = {
    "ACMECO_INFO_EMAIL_PASSWORD": "launch-acme-mail",
    "ACMECO_STRIPE_RESTRICTED": "launch-acme-stripe",
}

# Admission policy. Not credentials, so no secret scrub touches them, and a
# unit-file ``Environment=`` injects them without them ever appearing in a
# dotenv — so the strip is shape-matched, not name-listed.
LAUNCH_GATE_ENV = {
    "TELEGRAM_ALLOWED_USERS": "111",
    "DISCORD_ALLOW_ALL_USERS": "true",
    "SIGNAL_GROUP_ALLOW_FROM": "222",
}

# Worker scope the spawn env must keep — the scrub must not eat the task's own
# wiring. HERMES_KANBAN_* / TERMINAL_* are `_is_global_env` by prefix.
WORKER_SCOPE_KEYS = (
    "HERMES_HOME",
    "HERMES_PROFILE",
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_SESSION_SOURCE",
    "TERMINAL_CWD",
)


def _make_task(kb, *, assignee: str):
    return kb.Task(
        id="t_envisol",
        title="env isolation",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
    )


@pytest.fixture
def dispatcher(monkeypatch, tmp_path):
    """A dispatcher whose HERMES_HOME is the launch root, plus one named profile.

    Returns ``(kb, root, spawn)`` where ``spawn(assignee)`` runs the real
    ``_default_spawn`` and returns the env the child would have received.
    """
    root = tmp_path / ".hermes"
    (root / "profiles" / "elias").mkdir(parents=True)
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    # The launch profile's OWN dotenv. Its key names are what identifies
    # launch-profile residue; the values only have to be distinguishable.
    root.joinpath(".env").write_text(
        "".join(f"{k}='{v}'\n" for k, v in LAUNCH_DOTENV_COMPANY_ENV.items()),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name, value in {
        **DISPATCHER_CREDENTIAL_ENV,
        **LAUNCH_DOTENV_COMPANY_ENV,
        **LAUNCH_GATE_ENV,
    }.items():
        monkeypatch.setenv(name, value)

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_worker_hermes_argv", lambda: ["hermes"])
    # Keep the direct-Popen path: the systemd-scope launcher would not surface
    # the env dict this test inspects.
    monkeypatch.setattr(kb, "_spawn_gateway_scoped_worker", lambda *a, **k: None)

    captured: dict = {}

    class FakeProc:
        pid = 5150

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def spawn(assignee: str) -> dict:
        captured.clear()
        pid = kb._default_spawn(_make_task(kb, assignee=assignee), str(workspace))
        assert pid == 5150
        return captured["env"]

    return kb, root, spawn


def test_routed_profile_worker_gets_none_of_the_dispatcher_credentials(dispatcher):
    """The whole point: profile B's worker must not see the launch env's secrets."""
    _kb, root, spawn = dispatcher
    env = spawn("elias")

    leaked = {k: v for k, v in env.items() if k in DISPATCHER_CREDENTIAL_ENV}
    assert leaked == {}, f"dispatcher credentials reached another profile's worker: {sorted(leaked)}"

    # And the worker is still correctly pinned to its own lane.
    assert env["HERMES_HOME"] == str(root / "profiles" / "elias")
    assert env["HERMES_PROFILE"] == "elias"


def test_routed_worker_gets_no_operator_defined_company_credential(dispatcher):
    """The registry-derived scrub list cannot see these names; the residue strip can.

    Every key the LAUNCH profile's own ``.env`` defines is, by definition, the
    launch profile's configuration. A routed worker re-loads its own ``.env``,
    so anything it genuinely needs it supplies itself.
    """
    _kb, _root, spawn = dispatcher
    env = spawn("elias")
    leaked = sorted(k for k in env if k in LAUNCH_DOTENV_COMPANY_ENV)
    assert leaked == [], (
        f"launch-profile .env residue reached another lane's worker: {leaked}")


def test_routed_worker_gets_none_of_the_launch_admission_gates(dispatcher):
    """Allowlists and allow-all flags are profile policy, not process settings."""
    _kb, _root, spawn = dispatcher
    env = spawn("elias")
    leaked = sorted(k for k in env if k in LAUNCH_GATE_ENV)
    assert leaked == [], (
        f"the launch profile's admission policy reached another lane: {leaked}")


def test_launch_profile_worker_keeps_residue_and_gates(dispatcher):
    """Blast-radius bound: the launch profile's own worker is not stripped either."""
    _kb, _root, spawn = dispatcher
    env = spawn("default")
    for name, value in {**LAUNCH_DOTENV_COMPANY_ENV, **LAUNCH_GATE_ENV}.items():
        assert env.get(name) == value, (
            f"{name} must still reach the launch profile's own worker")


def test_routed_worker_keeps_its_whole_task_scope(dispatcher):
    """The scrub must not take the worker's own wiring with it."""
    _kb, _root, spawn = dispatcher
    env = spawn("elias")
    missing = [k for k in WORKER_SCOPE_KEYS if k not in env]
    assert missing == [], f"the scrub removed the worker's own task scope: {missing}"
    assert env["HERMES_KANBAN_TASK"] == "t_envisol"


def test_launch_profile_worker_env_is_unchanged(dispatcher):
    """The launch profile's OWN worker keeps the historical inherited env.

    ``resolve_profile_env("default")`` returns the launch root, so that worker
    is not routed and nothing is scrubbed. This is the blast-radius bound on
    the change: single-profile deployments are untouched.
    """
    _kb, root, spawn = dispatcher
    env = spawn("default")

    assert env["HERMES_HOME"] == str(root)
    for name, value in DISPATCHER_CREDENTIAL_ENV.items():
        assert env.get(name) == value, (
            f"{name} must still reach the launch profile's own worker")


def test_named_profile_with_no_home_still_gets_a_scrubbed_env(dispatcher):
    """Fail closed: an unprovable home is not evidence of launch ownership.

    ``resolve_profile_env`` raises ``FileNotFoundError`` for a NAMED profile
    with no directory on disk, and the old code then fell through with
    ``HERMES_HOME`` left as the dispatcher's. ``"default"`` always resolves, so
    a raise can only mean another lane — whose worker must not be handed the
    dispatcher's environment because its home could not be proven.
    """
    _kb, _root, spawn = dispatcher
    env = spawn("ghostlane")

    assert env["HERMES_PROFILE"] == "ghostlane"
    leaked = {k for k in env if k in DISPATCHER_CREDENTIAL_ENV}
    assert leaked == set(), f"a homeless named profile inherited: {sorted(leaked)}"


def test_passthrough_value_comes_from_the_assignee_scope_not_the_launch_env(
    monkeypatch, dispatcher, tmp_path,
):
    """An allowlisted variable crosses carrying the ASSIGNEE's value.

    ``_sanitize_subprocess_env`` resolves every ``terminal.env_passthrough``
    variable through ``get_secret``. Unscoped, that reads the dispatcher's
    ambient ``os.environ`` — the LAUNCH profile's value — for a worker spawned
    on another profile's behalf. ``_worker_profile_scope`` binds the
    assignee's own mapping around the build so the child gets its own lane's
    value instead.
    """
    from tools import env_passthrough

    _kb, root, spawn = dispatcher
    profile = root / "profiles" / "elias"
    profile.joinpath(".env").write_text(
        "COMPANY_LANE_KEY='lane-b-value'\n", encoding="utf-8")
    monkeypatch.setenv("COMPANY_LANE_KEY", "launch-value")
    # Which variables may cross is the DISPATCHER's policy; only their values
    # come from the assignee. Set the cache directly — the module memoizes the
    # config read process-wide.
    monkeypatch.setattr(
        env_passthrough, "_config_passthrough", frozenset({"COMPANY_LANE_KEY"}))

    env = spawn("elias")

    assert env["COMPANY_LANE_KEY"] == "lane-b-value", (
        "the worker must receive its own profile's value, not the launch profile's")


def test_routed_home_detection_treats_the_launch_home_as_its_own(monkeypatch, tmp_path):
    """Unit contract for the authority test, including its fail-closed default."""
    root = tmp_path / ".hermes"
    (root / "profiles" / "b").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli.kanban_db import _worker_targets_routed_home

    assert _worker_targets_routed_home("default", str(root)) is False
    # Same home reached by a different spelling is still not routed.
    assert _worker_targets_routed_home("default", str(root) + "/.") is False
    assert _worker_targets_routed_home("b", str(root / "profiles" / "b")) is True
    # No home resolved: "default" always resolves, so this is another lane.
    assert _worker_targets_routed_home("b", None) is True
    assert _worker_targets_routed_home("default", None) is False
