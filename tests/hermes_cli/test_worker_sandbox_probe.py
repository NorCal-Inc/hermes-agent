"""Tests for the worker-sandbox probe (C1.2a): check table, runner exit codes, one real bwrap run."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import worker_sandbox as ws

HOME = Path("/home/chris")
PROFILE = HOME / ".hermes" / "profiles" / "triptracker_lead"


def _manifest(tmp_path: Path, **overrides) -> dict:
    m = {
        "version": 1,
        "sandbox_home_root": str(tmp_path / "homes"),
        "shared_ro": [str(HOME / ".hermes" / "doctrine"), str(HOME / ".hermes" / "skills")],
        "credentials_ro": [str(HOME / ".hermes" / "auth.json")],
        "groups": {"logos_triptracker": ["logos", "triptracker"]},
        "lanes": {
            "logos": {"repos": ["/home/chris/hermes/repos/Logos-Covenant"], "vault": []},
            "triptracker": {
                "repos": ["/home/chris/TripTrackertechnologies"],
                "vault": ["/home/chris/services/life-wiki/vault/Business/Companies/TripTracker"],
            },
            "orion": {
                "repos": ["/home/chris/hermes/repos/OrionFormationServicesInc"],
                "vault": ["/home/chris/services/life-wiki/vault/Business/Companies/Orion-Formation-Services"],
            },
        },
    }
    m.update(overrides)
    return m


def _checks(tmp_path: Path, lane="triptracker", **kw):
    return ws.probe_checks(
        lane, "triptracker_lead", PROFILE, tmp_path / "ws", kw.pop("manifest", None) or _manifest(tmp_path),
        kanban_db_dir=kw.pop("kanban_db_dir", str(tmp_path / "kanban")), home=HOME, uid=1000,
        outside_pids=kw.pop("outside_pids", [4242]), exists=kw.pop("exists", lambda p: True),
        listdir=kw.pop("listdir", lambda d: ["orion_formation_services_lead", "triptracker_lead", "erika"]),
        **kw,
    )


def _by_name(checks):
    return {c["name"]: c for c in checks}


# ---------------------------------------------------------------- check table

def test_hidden_checks_cover_every_required_surface(tmp_path):
    c = _by_name(_checks(tmp_path))
    hidden = {n: v for n, v in c.items() if v["kind"] == "hidden"}
    joined = json.dumps(hidden)
    for frag in (
        "/.hermes/secrets", "Orion.env", "op-service-account-northcaledonia_lead.env",
        "/var/run/docker.sock", "/run/user/1000/bus", "/home/chris/.ssh", "/home/chris/.claude.json",
        "/home/chris/.codex", "op_cache.json", "/home/chris/.hermes/.env",
        "/home/chris/.hermes/profiles/orion_formation_services_lead", "/home/chris/.hermes/profiles/erika",
        "OrionFormationServicesInc", "Orion-Formation-Services",
    ):
        assert frag in joined, frag
    # own profile is never in the hidden list
    assert str(PROFILE) not in joined
    assert c["outside_pid_4242_environ_hidden"]["kind"] == "pid_hidden"
    assert c["outside_pid_4242_environ_hidden"]["target"] == 4242


def test_own_and_group_peer_paths_are_not_hidden(tmp_path):
    c = _by_name(_checks(tmp_path))
    other = c["other_lane_repos_and_vault_hidden"]["target"]
    assert "/home/chris/TripTrackertechnologies" not in other
    assert "/home/chris/hermes/repos/Logos-Covenant" not in other  # group peer
    assert "/home/chris/hermes/repos/OrionFormationServicesInc" in other
    orion = _by_name(_checks(tmp_path, lane="orion"))["other_lane_repos_and_vault_hidden"]["target"]
    assert "/home/chris/TripTrackertechnologies" in orion
    assert "/home/chris/hermes/repos/Logos-Covenant" in orion
    assert "/home/chris/hermes/repos/OrionFormationServicesInc" not in orion


def test_readonly_checks_follow_manifest_binding(tmp_path):
    c = _by_name(_checks(tmp_path))
    assert c["runtime_repo_readonly"]["kind"] == "readonly"
    assert c["runtime_repo_readonly"]["target"] == ws.runtime_code_paths(HOME)[0]
    assert c["hermes_skills_readonly"]["kind"] == "readonly"  # bound ro by the manifest
    assert c["npm_hidden"]["kind"] == "hidden"  # not bound at all
    assert c["credential_auth.json_readonly"] == {
        "name": "credential_auth.json_readonly", "kind": "readonly",
        "target": str(HOME / ".hermes" / "auth.json"), "file": True}
    m = _manifest(tmp_path, shared_ro=[])
    m["lanes"]["triptracker"]["extra_rw"] = [str(HOME / ".npm")]
    c2 = _by_name(_checks(tmp_path, manifest=m))
    assert "hermes_skills_hidden" in c2 and "hermes_skills_readonly" not in c2
    assert "npm_hidden" not in c2 and "npm_readonly" not in c2


def test_must_work_checks(tmp_path):
    c = _by_name(_checks(tmp_path))
    assert c["profile_dir_rw"] == {"name": "profile_dir_rw", "kind": "rw", "target": str(PROFILE)}
    assert c["workspace_rw"]["target"] == str(tmp_path / "ws")
    assert c["kanban_db_begin_immediate"] == {
        "name": "kanban_db_begin_immediate", "kind": "kanban_lock", "target": str(tmp_path / "kanban")}
    assert c["dns_resolve"]["target"] == "chatgpt.com"
    assert c["https_head"]["target"] == "https://chatgpt.com"
    assert c["op_service_account_token_absent"] == {
        "name": "op_service_account_token_absent", "kind": "env_absent", "target": "OP_SERVICE_ACCOUNT_TOKEN"}
    assert c["hermes_env_sourced_set"] == {
        "name": "hermes_env_sourced_set", "kind": "env_equals", "target": "HERMES_ENV_SOURCED", "value": "1"}
    no_net = _by_name(_checks(tmp_path, network=False))
    assert "dns_resolve" not in no_net and "https_head" not in no_net
    no_db = _by_name(_checks(tmp_path, kanban_db_dir=None))
    assert "kanban_db_begin_immediate" not in no_db


def test_check_table_carries_no_values_only_names_and_paths(tmp_path):
    for c in _checks(tmp_path):
        assert set(c) <= {"name", "kind", "target", "file", "value"}
        assert c["kind"] in ("hidden", "readonly", "rw", "kanban_lock", "pid_hidden", "dns",
                             "https_head", "env_absent", "env_equals")


# ---------------------------------------------------------------- runner / exit code

def _stub(results, returncode=0, stderr=""):
    calls = []

    def runner(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=returncode, stdout=json.dumps(results) if results is not None else "",
                               stderr=stderr)

    runner.calls = calls
    return runner


def test_run_probe_all_pass_exit_zero(tmp_path):
    checks = _checks(tmp_path, network=False)
    runner = _stub([{"name": c["name"], "ok": True, "detail": ""} for c in checks])
    res = ws.run_probe(["bwrap", "--x"], checks, runner=runner, python="/usr/bin/python3")
    assert res["ok"] is True and res["exit_code"] == 0
    assert [r["name"] for r in res["results"]] == [c["name"] for c in checks]
    argv, kw = runner.calls[0]
    assert argv[:3] == ["bwrap", "--x", "--"]
    assert argv[3:6] == ["/usr/bin/python3", "-I", "-c"]
    assert json.loads(argv[-1]) == checks
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in kw["env"]
    out = ws.format_probe(res)
    assert out.splitlines()[-1].startswith("PASS:")
    assert "FAIL" not in out


def test_run_probe_single_failure_exit_one(tmp_path):
    checks = _checks(tmp_path, network=False)
    results = [{"name": c["name"], "ok": True, "detail": ""} for c in checks]
    results[3]["ok"] = False
    results[3]["detail"] = "visible: 1 path(s)"
    res = ws.run_probe(["bwrap"], checks, runner=_stub(results))
    assert res["ok"] is False and res["exit_code"] == 1
    out = ws.format_probe(res)
    assert f"FAIL  {checks[3]['name']}" in out
    assert out.splitlines()[-1].startswith("FAIL:")


def test_run_probe_missing_result_counts_as_failure(tmp_path):
    checks = _checks(tmp_path, network=False)
    results = [{"name": c["name"], "ok": True, "detail": ""} for c in checks[1:]]
    res = ws.run_probe(["bwrap"], checks, runner=_stub(results))
    assert res["exit_code"] == 1
    assert res["results"][0]["detail"] == "no result"


def test_run_probe_sandbox_crash_exit_three(tmp_path):
    checks = _checks(tmp_path, network=False)
    res = ws.run_probe(["bwrap"], checks, runner=_stub(None, returncode=1, stderr="bwrap: boom"))
    assert res["exit_code"] == 3 and res["results"] == []
    assert "boom" in res["error"]

    def raising(*a, **k):
        raise subprocess.TimeoutExpired(cmd="bwrap", timeout=1)

    res = ws.run_probe(["bwrap"], checks, runner=raising)
    assert res["exit_code"] == 3 and "TimeoutExpired" in res["error"]


# ---------------------------------------------------------------- inner script, no bwrap

def test_probe_script_evaluates_checks_in_plain_process(tmp_path):
    rwdir = tmp_path / "rw"
    rwdir.mkdir()
    (tmp_path / "kanban").mkdir()
    sqlite3.connect(tmp_path / "kanban" / "kanban.db").close()
    checks = [
        {"name": "absent_hidden", "kind": "hidden", "target": [str(tmp_path / "nope"), str(tmp_path / "nope2")]},
        {"name": "present_not_hidden", "kind": "hidden", "target": [str(rwdir)]},
        {"name": "rw_ok", "kind": "rw", "target": str(rwdir)},
        {"name": "rw_missing", "kind": "rw", "target": str(tmp_path / "missing")},
        {"name": "ro_unbound", "kind": "readonly", "target": str(tmp_path / "missing")},
        {"name": "kanban", "kind": "kanban_lock", "target": str(tmp_path / "kanban")},
        {"name": "pid_self_visible", "kind": "pid_hidden", "target": os.getpid()},
        {"name": "env_absent", "kind": "env_absent", "target": "HERMES_PROBE_TEST_ABSENT_VAR"},
        {"name": "env_equals", "kind": "env_equals", "target": "HERMES_PROBE_TEST_VAR", "value": "1"},
        {"name": "bogus", "kind": "nope", "target": "x"},
    ]
    env = {"PATH": os.environ.get("PATH", "/usr/bin"), "HERMES_PROBE_TEST_VAR": "1"}
    proc = subprocess.run(["/usr/bin/python3", "-I", "-c", ws.PROBE_SCRIPT, json.dumps(checks)],
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    got = {r["name"]: r for r in json.loads(proc.stdout)}
    assert got["absent_hidden"]["ok"] is True
    assert got["present_not_hidden"]["ok"] is False
    assert got["rw_ok"]["ok"] is True
    assert got["rw_missing"]["ok"] is False and got["rw_missing"]["detail"] == "ENOENT"
    assert got["ro_unbound"]["ok"] is False
    assert got["kanban"]["ok"] is True
    assert got["pid_self_visible"]["ok"] is False  # same pid namespace -> visible
    assert got["env_absent"]["ok"] is True
    assert got["env_equals"]["ok"] is True
    assert got["bogus"]["ok"] is False
    assert not list(rwdir.iterdir())  # temp file removed


# ---------------------------------------------------------------- real bwrap (optional)

REAL_LANES = [("triptracker", "triptracker_lead"), ("orion", "orion_formation_services_lead")]


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed")
@pytest.mark.skipif(Path.home() != HOME, reason="test assumes the hermes host home")
@pytest.mark.parametrize("lane,profile", REAL_LANES)
def test_real_probe_passes_with_temp_manifest_and_scratch_dirs(tmp_path, lane, profile):
    for d in ("ws", "kanban", "profile", f"homes/{lane}"):
        (tmp_path / d).mkdir(parents=True)
    sqlite3.connect(tmp_path / "kanban" / "kanban.db").close()
    m = _manifest(tmp_path)
    m["lanes"] = {k: v for k, v in m["lanes"].items() if os.path.isdir(v["repos"][0])} or m["lanes"]
    m["shared_ro"] = [p for p in m["shared_ro"] if os.path.isdir(p)]
    m["credentials_ro"] = [p for p in m["credentials_ro"] if os.path.isfile(p)]
    if lane not in m["lanes"]:
        pytest.skip(f"lane {lane} repo not present on this host")
    prefix = ws.build_bwrap_argv(lane, profile, tmp_path / "profile", tmp_path / "ws", m,
                                 kanban_db_dir=tmp_path / "kanban", home=HOME)
    checks = ws.probe_checks(lane, profile, tmp_path / "profile", tmp_path / "ws", m,
                             kanban_db_dir=tmp_path / "kanban", home=HOME, outside_pids=[os.getpid()],
                             network=False)
    res = ws.run_probe(prefix, checks, python=ws._probe_python(HOME))
    assert res["exit_code"] == 0, ws.format_probe(res)
    assert not list((tmp_path / "ws").iterdir()) and not list((tmp_path / "profile").iterdir())


def test_main_requires_manifest_and_known_lane(tmp_path, capsys):
    (tmp_path / "ws").mkdir()
    rc = ws.main(["probe", "--profile", "x", "--workspace", str(tmp_path / "ws"),
                  "--manifest", str(tmp_path / "absent.yaml")])
    assert rc == ws.EXIT_PROBE_CONFIG
    mf = tmp_path / "m.yaml"
    mf.write_text("lanes:\n  logos:\n    repos: []\n", encoding="utf-8")
    rc = ws.main(["probe", "--profile", "x", "--workspace", str(tmp_path / "ws"), "--manifest", str(mf),
                  "--lane", "orion", "--profile-home", str(tmp_path / "ws")])
    assert rc in (ws.EXIT_PROBE_CONFIG, ws.EXIT_PROBE_RUNNER)  # unknown lane (or bwrap absent)
    err = capsys.readouterr().err
    assert "probe:" in err
