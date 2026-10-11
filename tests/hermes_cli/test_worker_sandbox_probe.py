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
    # C1.2d: the root store is never bound as-is; a codex lane sees the trimmed copy at that path.
    assert "credential_auth.json_readonly" not in c
    assert c["trimmed_codex_store_readonly"] == {
        "name": "trimmed_codex_store_readonly", "kind": "readonly",
        "target": str(HOME / ".hermes" / "auth.json"), "file": True}
    assert c["sandbox_no_refresh_set"] == {
        "name": "sandbox_no_refresh_set", "kind": "env_equals", "target": "HERMES_SANDBOX_NO_REFRESH", "value": "1"}
    mc = _manifest(tmp_path)
    mc["lanes"]["triptracker"]["llm_source"] = "claude_code"
    cc = _by_name(_checks(tmp_path, manifest=mc))
    assert "trimmed_codex_store_readonly" not in cc
    assert cc["root_auth_store_hidden"] == {
        "name": "root_auth_store_hidden", "kind": "hidden", "target": [str(HOME / ".hermes" / "auth.json")]}
    m = _manifest(tmp_path, shared_ro=[])
    m["lanes"]["triptracker"]["extra_rw"] = [str(HOME / ".npm")]
    c2 = _by_name(_checks(tmp_path, manifest=m))
    assert "hermes_skills_hidden" in c2 and "hermes_skills_readonly" not in c2
    assert "npm_hidden" not in c2 and "npm_readonly" not in c2
    assert not [n for n in c2 if n.startswith("skill_root_")]


def test_skill_roots_get_readonly_rows_when_present(tmp_path):
    roots = [str(HOME / ".claude" / "plugins"), str(HOME / ".claude" / "settings.json"), str(HOME / ".agents")]
    c = _by_name(_checks(tmp_path, manifest=_manifest(tmp_path, skill_roots_ro=roots)))
    assert c["skill_root_claude_plugins_readonly"] == {
        "name": "skill_root_claude_plugins_readonly", "kind": "readonly", "target": roots[0], "file": False}
    assert c["skill_root_claude_settings.json_readonly"] == {
        "name": "skill_root_claude_settings.json_readonly", "kind": "readonly", "target": roots[1], "file": True}
    assert c["skill_root_agents_readonly"] == {
        "name": "skill_root_agents_readonly", "kind": "readonly", "target": roots[2], "file": False}
    # still hidden: the rest of ~/.claude's neighbours and ~/.codex
    assert c["claude_json_hidden"]["kind"] == "hidden" and c["codex_dir_hidden"]["kind"] == "hidden"
    with pytest.raises(ws.SandboxConfigError):
        _checks(tmp_path, manifest=_manifest(tmp_path, skill_roots_ro=[str(HOME / ".claude" / "projects")]))
    for bad in ("/home/chris/.claude/plugins", {}, 0, False, [1]):
        with pytest.raises(ws.SandboxConfigError):
            _checks(tmp_path, manifest=_manifest(tmp_path, skill_roots_ro=bad))


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
    outside = ws.outside_processes([os.getpid()])
    for c in _checks(tmp_path, outside_pids=[4242] + outside):
        assert set(c) <= {"name", "kind", "target", "file", "value", "identity"}
        assert c["kind"] in ("hidden", "readonly", "rw", "kanban_lock", "pid_hidden", "dns",
                             "https_head", "env_absent", "env_equals")
        if "identity" in c:
            # a clock-tick count and a hash: never argv, never environ
            assert set(c["identity"]) == {"starttime", "cmdline_sha256"}
            assert c["identity"]["starttime"].isdigit()
            assert len(c["identity"]["cmdline_sha256"]) == 64


# ---------------------------------------------------------------- outside-pid identity (t_338366fd fix)

def test_process_identity_reads_starttime_and_cmdline_hash(tmp_path):
    ident = ws.process_identity(os.getpid())
    assert ident is not None and set(ident) == {"starttime", "cmdline_sha256"}
    assert ident["starttime"].isdigit() and len(ident["cmdline_sha256"]) == 64
    # stable across two reads of the same live process
    assert ws.process_identity(os.getpid()) == ident
    assert ws.process_identity(2 ** 22 - 1) is None or isinstance(ws.process_identity(2 ** 22 - 1), dict)
    # comm with spaces/parens does not shift the starttime field (synthetic /proc)
    fake = tmp_path / "proc" / "77"
    fake.mkdir(parents=True)
    (fake / "stat").write_text("77 (we ird) name) S 1 77 77 0 -1 4194560 1 0 0 0 0 0 0 0 20 0 1 0 123456 1 1 "
                               "18446744073709551615 1 1 0 0 0 0 0 0 0 0 0 0 0 17 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n")
    (fake / "cmdline").write_bytes(b"x\0y\0")
    got = ws.process_identity(77, proc=str(tmp_path / "proc"))
    assert got["starttime"] == "123456"
    assert got["cmdline_sha256"] == __import__("hashlib").sha256(b"x\0y\0").hexdigest()


def test_outside_processes_skips_pid_1_and_2_and_unreadable(tmp_path):
    me = os.getpid()
    recs = ws.outside_processes([1, 2, None, 0, "x", me, me, 2 ** 22 - 1])
    assert [r["pid"] for r in recs] == [me] or [r["pid"] for r in recs] == [me, 2 ** 22 - 1]
    assert recs[0]["identity"] == ws.process_identity(me)
    assert not [r for r in recs if r["pid"] in (0, 1, 2)]
    # records flow into the check table as pid_hidden rows carrying the identity
    c = _by_name(_checks(tmp_path, outside_pids=recs))
    row = c[f"outside_pid_{me}_environ_hidden"]
    assert row["kind"] == "pid_hidden" and row["target"] == me and row["identity"] == recs[0]["identity"]


def _run_probe_script(checks, env=None):
    env = env or {"PATH": os.environ.get("PATH", "/usr/bin")}
    proc = subprocess.run(["/usr/bin/python3", "-I", "-c", ws.PROBE_SCRIPT, json.dumps(checks)],
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    return {r["name"]: r for r in json.loads(proc.stdout)}


def test_pid_hidden_number_collision_with_different_identity_is_not_visible():
    """The verifier's false 'visible': the OUTSIDE pid number exists inside the sandbox too,
    but it is a different process. Here the current process stands in for the colliding
    in-sandbox pid; the recorded identity is of some other (fabricated) outside process."""
    me = os.getpid()
    real = ws.process_identity(me)
    forged = {"starttime": str(int(real["starttime"]) + 1), "cmdline_sha256": "0" * 64}
    got = _run_probe_script([
        {"name": "collide_diff_identity", "kind": "pid_hidden", "target": me, "identity": forged},
        {"name": "collide_diff_starttime_only", "kind": "pid_hidden", "target": me,
         "identity": {"starttime": forged["starttime"], "cmdline_sha256": real["cmdline_sha256"]}},
        {"name": "collide_diff_cmdline_only", "kind": "pid_hidden", "target": me,
         "identity": {"starttime": real["starttime"], "cmdline_sha256": forged["cmdline_sha256"]}},
        {"name": "same_identity_visible", "kind": "pid_hidden", "target": me, "identity": real},
        {"name": "legacy_number_only_visible", "kind": "pid_hidden", "target": me},
        {"name": "absent_pid_hidden", "kind": "pid_hidden", "target": 2 ** 22 - 1, "identity": forged},
    ])
    assert got["collide_diff_identity"]["ok"] is True
    assert "identity differs" in got["collide_diff_identity"]["detail"]
    assert got["collide_diff_starttime_only"]["ok"] is True
    assert got["collide_diff_cmdline_only"]["ok"] is True
    # the same process, really visible in the same namespace, is still caught
    assert got["same_identity_visible"]["ok"] is False
    assert got["legacy_number_only_visible"]["ok"] is False
    assert got["absent_pid_hidden"]["ok"] is True


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
    # C1.2d: a codex lane needs the gateway-materialised trimmed file under the REAL
    # home, which a test must never create; run the live probe as a claude_code lane
    # (root store hidden) and leave the codex bind to the unit tests.
    m["lanes"][lane]["llm_source"] = "claude_code"
    prefix = ws.build_bwrap_argv(lane, profile, tmp_path / "profile", tmp_path / "ws", m,
                                 kanban_db_dir=tmp_path / "kanban", home=HOME)
    # Outside processes by identity, recorded before bwrap: when this test itself runs in a
    # pid namespace (small pid numbers) the number alone would collide with the probe inside.
    outside = ws.outside_processes([os.getpid(), os.getppid(), ws._gateway_pid()])
    assert outside and outside[0]["pid"] == os.getpid() or os.getpid() <= 2
    checks = ws.probe_checks(lane, profile, tmp_path / "profile", tmp_path / "ws", m,
                             kanban_db_dir=tmp_path / "kanban", home=HOME, outside_pids=outside,
                             network=False)
    res = ws.run_probe(prefix, checks, python=ws._probe_python(HOME))
    assert res["exit_code"] == 0, ws.format_probe(res)
    assert [r for r in res["results"] if r["name"].startswith("outside_pid_")]
    assert not list((tmp_path / "ws").iterdir()) and not list((tmp_path / "profile").iterdir())


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed")
@pytest.mark.skipif(Path.home() != HOME, reason="test assumes the hermes host home")
def test_real_probe_pid_number_collision_inside_namespace_is_hidden(tmp_path):
    """Run the real inner script inside bwrap with a check for pid 2 (the sandboxed command
    itself) carrying the identity of an OUTSIDE pid-2-numbered or forged process: must be hidden."""
    for d in ("ws", "profile", "homes/triptracker"):
        (tmp_path / d).mkdir(parents=True)
    m = _manifest(tmp_path, shared_ro=[], credentials_ro=[])
    m["lanes"] = {"triptracker": {"repos": [], "vault": [], "llm_source": "claude_code"}}
    prefix = ws.build_bwrap_argv("triptracker", "triptracker_lead", tmp_path / "profile", tmp_path / "ws", m, home=HOME)
    forged = {"starttime": "1", "cmdline_sha256": "0" * 64}
    checks = [
        {"name": "pid2_forged_identity", "kind": "pid_hidden", "target": 2, "identity": forged},
        {"name": "pid1_forged_identity", "kind": "pid_hidden", "target": 1, "identity": forged},
        {"name": "pid2_number_only_legacy", "kind": "pid_hidden", "target": 2},
    ]
    res = ws.run_probe(prefix, checks, python=ws._probe_python(HOME))
    got = {r["name"]: r for r in res["results"]}
    assert got["pid2_forged_identity"]["ok"] is True, got
    assert got["pid1_forged_identity"]["ok"] is True, got
    # and this is exactly the false "visible" the number-only check produced (t_338366fd)
    assert got["pid2_number_only_legacy"]["ok"] is False


SKILL_TRUST_VERIFY = HOME / ".hermes" / "security" / "skill-trust" / "verify.py"


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed")
@pytest.mark.skipif(Path.home() != HOME, reason="test assumes the hermes host home")
@pytest.mark.skipif(not SKILL_TRUST_VERIFY.is_file(), reason="skill-trust verifier not present")
def test_real_skill_trust_gate_passes_with_skill_roots_ro(tmp_path):
    """verify.py exits 0 inside the sandbox once the three skill roots are ro-bound (C1.2c)."""
    for d in ("ws", "profile", "homes/triptracker"):
        (tmp_path / d).mkdir(parents=True)
    roots = [str(HOME / ".claude" / "plugins"), str(HOME / ".claude" / "settings.json"), str(HOME / ".agents")]
    if not all(os.path.exists(p) for p in roots):
        pytest.skip("skill roots not all present on this host")
    m = _manifest(tmp_path, shared_ro=[str(HOME / ".hermes" / "skills"), str(SKILL_TRUST_VERIFY.parent)],
                  credentials_ro=[], skill_roots_ro=roots)
    m["lanes"] = {"triptracker": {"repos": [], "vault": [], "llm_source": "claude_code"}}
    prefix = ws.build_bwrap_argv("triptracker", "triptracker_lead", tmp_path / "profile", tmp_path / "ws", m, home=HOME)
    for p in roots:
        assert prefix[prefix.index(p) - 1] == "--ro-bind"
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
    res = subprocess.run(prefix + ["--", "/usr/bin/python3", "-I", str(SKILL_TRUST_VERIFY)],
                         capture_output=True, text=True, timeout=300, env=env)
    assert res.returncode == 0, (res.stdout.strip().splitlines() or [""])[0]
    assert res.stdout.startswith("SKILL TRUST: PASS")
    # the probe's own MUST-BE-READ-ONLY rows for the three roots pass in there too
    checks = [c for c in ws.probe_checks("triptracker", "triptracker_lead", tmp_path / "profile", tmp_path / "ws", m,
                                         home=HOME, network=False) if c["name"].startswith("skill_root_")]
    assert len(checks) == 3
    pr = ws.run_probe(prefix, checks, python=ws._probe_python(HOME))
    assert pr["exit_code"] == 0, ws.format_probe(pr)
    # and the roots are genuinely read-only in there
    write = subprocess.run(prefix + ["--", "/bin/sh", "-c",
                           "touch %s/.probe 2>/dev/null && echo WROTE; touch %s/.probe 2>/dev/null && echo WROTE; "
                           "touch %s 2>/dev/null && echo WROTE; exit 0" % (roots[0], roots[2], roots[1])],
                           capture_output=True, text=True, timeout=60, env=env)
    assert "WROTE" not in write.stdout


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
