"""Tests for hermes_cli.skill_bridge (kanban t_9c745fdc).

Vault-first learned-skill bridge + need-based profile provisioning:

* provisioning copies ONLY approved baseline bytes (root tree pinned in the
  skill-trust registry, or an attested company-lane baseline), verifies the
  hash, records provenance, never overwrites, and fails closed on
  unregistered / drifted / cross-lane / diverged-target cases;
* the dispatcher turns a refusal into a typed ``capability`` block (Erika's
  provisioning path) that keeps the assignee, and auto-provisions a missing
  approved shared skill before spawning the same worker;
* the vault half posts PENDING observations through the supported
  ``/api/skills`` boundary and promotes to doctrine only with verification
  evidence (verified kanban task or explicit attestation), recording a
  bidirectional ledger pointer; staged Hermes skill writes mirror into the
  vault as pending observations; runtime projections are STAGED for review,
  never written.

Every fixture pins its own Hermes root and kanban env (mistake-log row
2026-10-07): nothing here can touch the live board or the live skill trees.
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import skill_bridge as sb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_skill(base: Path, rel: str, body: str = "body\n") -> Path:
    d = base / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {d.name}\ndescription: test {d.name}\n---\n{body}", encoding="utf-8")
    (d / "references").mkdir(exist_ok=True)
    (d / "references" / "note.md").write_text(f"ref for {d.name}\n", encoding="utf-8")
    return d


def _mk_profile(root: Path, name: str, company: str) -> Path:
    p = root / "profiles" / name
    (p / "skills").mkdir(parents=True)
    (p / "profile.yaml").write_text(f"company: {company}\n", encoding="utf-8")
    return p


@pytest.fixture
def bridge_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated Hermes root with a root skill tree, a fake skill-trust
    registry, three profiles (two company lanes + shared) and a sandboxed
    kanban board."""
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Kanban isolation — pin the sandbox home and drop every dispatcher pin
    # that could outrank it (the 2026-10-07 live-board leak).
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_LOGS_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_SKILL_BRIDGE_DIR", str(tmp_path / "bridge"))
    monkeypatch.delenv("SKILL_VERIFIER_TOKEN", raising=False)

    skills = root / "skills"
    skill_a = _write_skill(skills, "devops/skill-a")
    _write_skill(skills, "devops/unreg")           # on disk, not in registry
    _write_skill(skills, "marketing/dup-name")      # ambiguity pair
    _write_skill(skills, "media/dup-name")

    registry = {
        "version": 1, "policy": "frozen-after-review-no-upstream-update",
        "items": [{
            "name": "skill-a", "path": str(skill_a), "skill_key": "hermes:devops/skill-a",
            "sha256_tree": sb.tree_hash(skill_a), "attested_at": "2026-09-19",
            "status": "current-bytes-attested-2026-09-19",
        }],
    }
    reg_path = tmp_path / "registry.json"
    reg_path.write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setenv("HERMES_SKILL_TRUST_REGISTRY", str(reg_path))

    _mk_profile(root, "acme_lead", "acme")
    _mk_profile(root, "acme_worker", "acme")
    _mk_profile(root, "other_lead", "other")
    _mk_profile(root, "shared_w", "shared")

    kb.init_db()
    return {"root": root, "skills": skills, "skill_a": skill_a, "registry": reg_path,
            "tmp": tmp_path}


# ---------------------------------------------------------------------------
# Hashing and lookup
# ---------------------------------------------------------------------------

def test_tree_hash_is_deterministic_and_detects_any_byte_change(bridge_env):
    d = bridge_env["skill_a"]
    h1 = sb.tree_hash(d)
    assert h1 == sb.tree_hash(d)
    (d / "references" / "note.md").write_text("changed\n", encoding="utf-8")
    assert sb.tree_hash(d) != h1
    # bytecode artefacts are ignored, exactly like security/skill-trust/verify.py
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    h2 = sb.tree_hash(d)
    (d / "__pycache__" / "x.pyc").write_bytes(b"\x01\x02")
    assert sb.tree_hash(d) == h2


def test_find_skill_dir_bare_qualified_dotdirs_and_ambiguity(bridge_env):
    skills = bridge_env["skills"]
    assert sb.find_skill_dir(skills, "skill-a")[0] == bridge_env["skill_a"]
    assert sb.find_skill_dir(skills, "devops/skill-a")[0] == bridge_env["skill_a"]
    assert sb.find_skill_dir(skills, "nope") == (None, sb.REFUSED_UNKNOWN_SKILL)
    assert sb.find_skill_dir(skills, "dup-name") == (None, sb.REFUSED_AMBIGUOUS)
    assert sb.find_skill_dir(skills, "marketing/dup-name")[0] is not None
    _write_skill(skills, ".archive/hidden-skill")
    assert sb.find_skill_dir(skills, "hidden-skill") == (None, sb.REFUSED_UNKNOWN_SKILL)


# ---------------------------------------------------------------------------
# Provisioning from the approved root baseline
# ---------------------------------------------------------------------------

def test_provision_copies_exact_registry_bytes_and_records_provenance(bridge_env):
    r = sb.provision_skill("skill-a", "acme_lead", task_id="t_test1", by="test")
    assert r.status == sb.STATUS_PROVISIONED, r
    target = Path(r.target_path)
    assert target == bridge_env["root"] / "profiles" / "acme_lead" / "skills" / "devops" / "skill-a"
    assert (target / "SKILL.md").is_file()
    assert sb.tree_hash(target) == sb.tree_hash(bridge_env["skill_a"]) == r.sha256_tree
    prov = json.loads((target.parent.parent / sb.PROVENANCE_FILE).read_text())
    assert prov["skill-a"]["sha256_tree"] == r.sha256_tree
    assert prov["skill-a"]["source"] == "root-registry"
    assert prov["skill-a"]["task_id"] == "t_test1"
    ledger = sb._read_jsonl(sb.bridge_dir() / sb.LEDGER_FILE)
    assert ledger and ledger[-1]["event"] == "provision" and ledger[-1]["profile"] == "acme_lead"
    # Source untouched (frozen stays frozen).
    assert sb.tree_hash(bridge_env["skill_a"]) == json.loads(bridge_env["registry"].read_text())["items"][0]["sha256_tree"]


def test_provision_is_idempotent_present_and_dry_run_writes_nothing(bridge_env):
    dry = sb.provision_skill("skill-a", "acme_lead", dry_run=True)
    assert dry.status == sb.STATUS_WOULD_PROVISION
    assert not Path(dry.target_path).exists()
    assert sb.provision_skill("skill-a", "acme_lead").status == sb.STATUS_PROVISIONED
    again = sb.provision_skill("skill-a", "acme_lead")
    assert again.status == sb.STATUS_PRESENT
    assert len([e for e in sb._read_jsonl(sb.bridge_dir() / sb.LEDGER_FILE) if e["event"] == "provision"]) == 1


def test_unregistered_root_skill_is_refused_and_nothing_is_copied(bridge_env):
    r = sb.provision_skill("unreg", "acme_lead")
    assert r.status == sb.STATUS_REFUSED and r.reason == sb.REFUSED_UNREGISTERED
    assert not list((bridge_env["root"] / "profiles" / "acme_lead" / "skills").rglob("SKILL.md"))


def test_root_drift_is_refused_not_copied(bridge_env):
    (bridge_env["skill_a"] / "SKILL.md").write_text("tampered\n", encoding="utf-8")
    r = sb.provision_skill("skill-a", "acme_lead")
    assert r.status == sb.STATUS_REFUSED and r.reason == sb.REFUSED_SOURCE_DRIFT
    assert not list((bridge_env["root"] / "profiles" / "acme_lead" / "skills").rglob("SKILL.md"))


def test_unknown_and_ambiguous_names_are_refused(bridge_env):
    assert sb.provision_skill("nope", "acme_lead").reason == sb.REFUSED_UNKNOWN_SKILL
    assert sb.provision_skill("dup-name", "acme_lead").reason == sb.REFUSED_AMBIGUOUS
    assert sb.provision_skill("skill-a", "ghost_profile").reason == sb.REFUSED_NO_PROFILE


def test_diverged_target_is_never_overwritten_and_never_removed(bridge_env):
    first = sb.provision_skill("skill-a", "acme_lead")
    target = Path(first.target_path)
    (target / "SKILL.md").write_text("profile-local edit\n", encoding="utf-8")
    r = sb.provision_skill("skill-a", "acme_lead")
    assert r.status == sb.STATUS_REFUSED and r.reason == sb.REFUSED_TARGET_DRIFT
    assert (target / "SKILL.md").read_text() == "profile-local edit\n"
    u = sb.unprovision_skill("skill-a", "acme_lead")
    assert u.status == sb.STATUS_REFUSED and u.reason == sb.REFUSED_TARGET_DRIFT
    assert (target / "SKILL.md").is_file()
    drift = sb.drift_report("acme_lead")
    assert len(drift) == 1 and drift[0]["status"] == "CHANGED"


def test_pre_bridge_profile_copy_without_baseline_is_left_alone(bridge_env):
    # A skill that exists only in the profile (no root, no attestation):
    # the worker can load it; the bridge neither copies nor refuses.
    _write_skill(bridge_env["root"] / "profiles" / "acme_lead" / "skills", "custom/acme-only")
    r = sb.provision_skill("acme-only", "acme_lead")
    assert r.status == sb.STATUS_PRESENT and "pre-bridge" in r.detail


def test_unprovision_rolls_back_a_matching_copy_and_clears_provenance(bridge_env):
    first = sb.provision_skill("skill-a", "acme_lead")
    u = sb.unprovision_skill("skill-a", "acme_lead", by="test")
    assert u.status == "unprovisioned"
    assert not Path(first.target_path).exists()
    assert sb._load_provenance(bridge_env["root"] / "profiles" / "acme_lead" / "skills") == {}
    assert sb.drift_report("acme_lead") == []
    assert sb.unprovision_skill("skill-a", "acme_lead").reason == sb.REFUSED_UNKNOWN_SKILL


def test_drift_report_flags_missing_and_changed(bridge_env):
    r1 = sb.provision_skill("skill-a", "acme_lead")
    r2 = sb.provision_skill("skill-a", "shared_w")
    assert sb.drift_report() and all(x["status"] == "OK" for x in sb.drift_report())
    (Path(r1.target_path) / "SKILL.md").write_text("x", encoding="utf-8")
    import shutil
    shutil.rmtree(r2.target_path)
    rows = {(x["profile"], x["status"]) for x in sb.drift_report()}
    assert rows == {("acme_lead", "CHANGED"), ("shared_w", "MISSING")}


# ---------------------------------------------------------------------------
# Company-lane baselines
# ---------------------------------------------------------------------------

def test_company_baseline_provisions_inside_lane_only(bridge_env):
    root = bridge_env["root"]
    _write_skill(root / "profiles" / "acme_lead" / "skills", "custom/acme-only", body="acme secret sauce\n")
    with pytest.raises(ValueError):
        sb.attest_company_baseline("skill-a", "shared_w", by="erika")  # shared lane: not a company baseline
    entry = sb.attest_company_baseline("acme-only", "acme_lead", by="erika", note="reviewed")
    assert entry["lane"] == "acme"

    ok = sb.provision_skill("acme-only", "acme_worker", task_id="t_acme")
    assert ok.status == sb.STATUS_PROVISIONED and ok.source == "company-baseline"
    assert sb.tree_hash(ok.target_path) == entry["sha256_tree"]

    cross = sb.provision_skill("acme-only", "other_lead")
    assert cross.status == sb.STATUS_REFUSED and cross.reason == sb.REFUSED_COMPANY_SCOPE
    assert not list((root / "profiles" / "other_lead" / "skills").rglob("SKILL.md"))

    shared = sb.provision_skill("acme-only", "shared_w")
    assert shared.status == sb.STATUS_REFUSED and shared.reason == sb.REFUSED_COMPANY_SCOPE

    # Attested bytes drift -> refused (company lane, same rule as root).
    (root / "profiles" / "acme_lead" / "skills" / "custom" / "acme-only" / "SKILL.md").write_text("mutated", encoding="utf-8")
    import shutil
    shutil.rmtree(ok.target_path)
    sb._save_provenance(root / "profiles" / "acme_worker" / "skills", {})
    drift = sb.provision_skill("acme-only", "acme_worker")
    assert drift.status == sb.STATUS_REFUSED and drift.reason == sb.REFUSED_SOURCE_DRIFT


def test_task_tenant_must_match_profile_lane(bridge_env):
    r = sb.provision_skill("skill-a", "acme_lead", task_tenant="other")
    assert r.status == sb.STATUS_REFUSED and r.reason == sb.REFUSED_COMPANY_SCOPE
    assert sb.provision_skill("skill-a", "acme_lead", task_tenant="acme").status == sb.STATUS_PROVISIONED
    # shared-lane profile accepts any tenant's task (Erika / direct executors)
    assert sb.provision_skill("skill-a", "shared_w", task_tenant="other").status == sb.STATUS_PROVISIONED


# ---------------------------------------------------------------------------
# Dispatcher preflight
# ---------------------------------------------------------------------------

def test_preflight_raises_typed_refusal_with_full_results(bridge_env):
    with pytest.raises(sb.SkillProvisionRefused) as ei:
        sb.preflight_task_skills(["skill-a", "unreg"], "acme_lead", task_id="t_pf")
    exc = ei.value
    assert "CAPABILITY_GAP" in str(exc) and "unreg" in str(exc)
    statuses = {r.skill: r.status for r in exc.results}
    assert statuses == {"skill-a": sb.STATUS_PROVISIONED, "unreg": sb.STATUS_REFUSED}
    assert exc.payload()["task_id"] == "t_pf"


def _spawn_with_preflight(spawns):
    def fake_spawn(task, workspace, board=None):
        sb.preflight_task_skills(list(task.skills or []), task.assignee,
                                 task_id=task.id, task_tenant=task.tenant)
        spawns.append(task.id)
        return 42
    return fake_spawn


def test_dispatch_provisions_approved_skill_then_spawns_same_worker(bridge_env):
    spawns = []
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="needs skill-a", assignee="acme_lead", skills=["skill-a"])
        kb.dispatch_once(conn, spawn_fn=_spawn_with_preflight(spawns))
        task = kb.get_task(conn, tid)
    assert spawns == [tid]
    assert task.status == "running" and task.assignee == "acme_lead"
    assert (bridge_env["root"] / "profiles" / "acme_lead" / "skills" / "devops" / "skill-a" / "SKILL.md").is_file()


def test_dispatch_refusal_becomes_capability_block_that_returns_to_same_worker(bridge_env):
    spawns = []
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="needs unreg", assignee="acme_lead", skills=["unreg"])
        res = kb.dispatch_once(conn, spawn_fn=lambda task, ws, board=None: _raise(task))
        task = kb.get_task(conn, tid)
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,)).fetchall()]
        payload = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='skill_provision_refused'", (tid,)
        ).fetchone()["payload"])
        assert spawns == []
        assert tid in res.auto_blocked
        assert task.status == "blocked" and task.block_kind == "capability"
        assert task.assignee == "acme_lead"            # Erika provisions; the same worker resumes
        assert "skill_provision_refused" in kinds
        assert payload["results"][0]["reason"] == sb.REFUSED_UNREGISTERED
        assert "spawn_failed" not in json.dumps([dict(r) for r in conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id=?", (tid,)).fetchall()])
        # Once provisioned (here: Erika approves by registering it), unblock returns it to the pool.
        assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.get_task(conn, tid).assignee == "acme_lead"


def _raise(task):
    results = [sb.ProvisionResult("unreg", task.assignee, sb.STATUS_REFUSED, sb.REFUSED_UNREGISTERED, "test")]
    raise sb.SkillProvisionRefused(results, profile=task.assignee, task_id=task.id)


def test_dispatch_real_refusal_path_end_to_end(bridge_env):
    spawns = []
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="needs unreg", assignee="acme_lead", skills=["unreg"])
        kb.dispatch_once(conn, spawn_fn=_spawn_with_preflight(spawns))
        task = kb.get_task(conn, tid)
    assert spawns == []
    assert task.status == "blocked" and task.block_kind == "capability"
    assert not list((bridge_env["root"] / "profiles" / "acme_lead" / "skills").rglob("SKILL.md"))


# ---------------------------------------------------------------------------
# Vault half — against a loopback stub of the LifeWiki /api/skills routes
# ---------------------------------------------------------------------------

class _VaultState:
    def __init__(self):
        self.token = "test-token"
        self.pending = {}      # obs_id -> (cap, lesson)
        self.doctrine = {}     # cap -> [lines]
        self.n = 0
        self.observe_bodies = []
        self.verify_bodies = []


def _make_handler(state: _VaultState):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def _send(self, code, obj):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            parts = self.path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "skills"]:
                cap = parts[2]
                self._send(200, {"capability": cap, "doctrine": state.doctrine.get(cap, []),
                                 "pending_count": sum(1 for c, _ in state.pending.values() if c == cap)})
            else:
                self._send(404, {"error": "nope"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            parts = self.path.strip("/").split("/")
            cap, verb = parts[2], parts[3]
            if verb == "observe":
                state.observe_bodies.append(body)
                state.n += 1
                oid = f"obs_test{state.n}"
                state.pending[oid] = (cap, body["lesson"])
                self._send(201, {"status": "pending", "obs_id": oid, "capability": cap})
            elif verb == "verify":
                if self.headers.get("x-verifier-token") != state.token:
                    self._send(403, {"error": "verifier token required"})
                    return
                state.verify_bodies.append(body)
                oid = body["obs_id"]
                if oid not in state.pending:
                    self._send(200, {"status": "not_found", "obs_id": oid})
                    return
                c, lesson = state.pending.pop(oid)
                if body["decision"] == "approve":
                    # Live API shape: doctrine entries are objects with a `text` field.
                    state.doctrine.setdefault(c, []).append({
                        "timestamp": "2026-10-12 22:01:42", "pattern": "", "confidence": 0.5,
                        "text": f"{lesson} (verified from {oid} by {body['verifier']})"})
                self._send(200, {"status": "approved" if body["decision"] == "approve" else "rejected",
                                 "obs_id": oid, "capability": c, "doctrine_appended": body["decision"] == "approve",
                                 "git": {"committed": True, "sha": "deadbeef"}})
            else:
                self._send(404, {"error": "nope"})
    return H


@pytest.fixture
def vault(bridge_env, monkeypatch):
    state = _VaultState()
    srv = HTTPServer(("127.0.0.1", 0), _make_handler(state))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    monkeypatch.setenv("LIFEWIKI_URL", f"http://127.0.0.1:{srv.server_port}")
    yield state
    srv.shutdown()


def test_observe_posts_pending_and_records_ledger(vault):
    out = sb.vault_observe("verification", "A lesson.", task="t_x", evidence="ev", source="hermes-learn")
    assert out["obs_id"] == "obs_test1" and out["status"] == "pending"
    assert vault.observe_bodies[0]["task"] == "t_x" and vault.observe_bodies[0]["source"] == "hermes-learn"
    rows = sb._read_jsonl(sb.bridge_dir() / sb.OBSERVATIONS_FILE)
    assert rows[-1]["event"] == "observe" and rows[-1]["obs_id"] == "obs_test1"
    assert rows[-1]["lesson_sha256"] == hashlib.sha256(b"A lesson.").hexdigest()
    # Nothing promoted: still pending, no doctrine.
    assert "obs_test1" in vault.pending and vault.doctrine == {}


def test_observe_rejects_bad_slugs_and_capabilities(vault):
    with pytest.raises(sb.VaultError):
        sb.vault_observe("Bad Cap", "x")
    with pytest.raises(sb.VaultError):
        sb.vault_observe("verification", "x", source="Not A Slug")
    with pytest.raises(sb.VaultError):
        sb.vault_observe("verification", "x", outcome="maybe")


def test_promote_fails_closed_without_token_or_evidence(vault, monkeypatch):
    oid = sb.vault_observe("verification", "lesson")["obs_id"]
    with pytest.raises(sb.VaultError, match="not set"):
        sb.vault_promote("verification", oid, verifier="erika", attested_by="erika", attestation_evidence="x")
    monkeypatch.setenv("SKILL_VERIFIER_TOKEN", vault.token)
    with pytest.raises(sb.VaultError, match="refusing promotion"):
        sb.vault_promote("verification", oid, verifier="erika")
    # Unverified kanban task (real sandbox board) -> refused; stays pending.
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="unverified", assignee="acme_lead")
    with pytest.raises(sb.VaultError, match="not verified"):
        sb.vault_promote("verification", oid, verifier="erika", task=tid)
    assert oid in vault.pending and vault.verify_bodies == []


def test_promote_with_verified_task_appends_doctrine_and_ledger_pointer(vault, monkeypatch):
    monkeypatch.setenv("SKILL_VERIFIER_TOKEN", vault.token)
    oid = sb.vault_observe("verification", "Verified lesson.")["obs_id"]
    monkeypatch.setattr(sb, "task_is_verified", lambda tid: (True, f"task {tid} verification_state=verified"))
    out = sb.vault_promote("verification", oid, verifier="gauntlet", task="t_ok", note="n")
    assert out["doctrine_appended"] is True
    assert vault.verify_bodies[-1]["note"].endswith("basis: task t_ok verification_state=verified")
    line = vault.doctrine["verification"][-1]["text"]
    rec = out["bridge"]
    assert rec["event"] == "promote" and rec["obs_id"] == oid and rec["task"] == "t_ok"
    assert rec["doctrine_line_sha256"] == hashlib.sha256(line.encode()).hexdigest()
    assert sb.doctrine_line_for("verification", oid) == line
    assert sb.doctrine_line_for("verification", "obs_nope") is None
    assert sb._read_jsonl(sb.bridge_dir() / sb.OBSERVATIONS_FILE)[-1]["event"] == "promote"
    with pytest.raises(sb.VaultError, match="not pending"):
        sb.vault_promote("verification", oid, verifier="gauntlet", task="t_ok")


def test_wrong_token_is_a_closed_gate(vault, monkeypatch):
    monkeypatch.setenv("SKILL_VERIFIER_TOKEN", "wrong")
    oid = sb.vault_observe("verification", "lesson")["obs_id"]
    with pytest.raises(sb.VaultError, match="403"):
        sb.vault_promote("verification", oid, verifier="erika", attested_by="erika", attestation_evidence="e")
    assert oid in vault.pending


def test_reject_removes_pending_without_doctrine(vault, monkeypatch):
    monkeypatch.setenv("SKILL_VERIFIER_TOKEN", vault.token)
    oid = sb.vault_observe("verification", "bad lesson")["obs_id"]
    out = sb.vault_reject("verification", oid, verifier="erika", note="no")
    assert out["status"] == "rejected" and oid not in vault.pending and vault.doctrine == {}


def _stage_record(home: Path, pid: str, name: str, category: str = "devops") -> Path:
    d = home / "pending" / "skills"
    d.mkdir(parents=True, exist_ok=True)
    rec = {"id": pid, "subsystem": "skills", "action": "create", "summary": f"gist of {name}",
           "origin": "foreground", "created_at": 1.0,
           "payload": {"action": "create", "name": name, "category": category, "content": "..."}}
    p = d / f"{pid}.json"
    p.write_text(json.dumps(rec), encoding="utf-8")
    return p


def test_stage_pending_mirrors_every_home_once_and_maps_capability(vault, bridge_env):
    root = bridge_env["root"]
    _stage_record(root, "aaaa0001", "ops-thing", "devops")
    _stage_record(root / "profiles" / "acme_lead", "bbbb0002", "mailer", "email")
    _stage_record(root / "profiles" / "acme_lead", "cccc0003", "misc", "whatever")
    rows = sb.stage_pending_to_vault(task="t_learn")
    assert {(r["home"], r["pending_id"], r["capability"]) for r in rows} == {
        ("root", "aaaa0001", "ops"), ("acme_lead", "bbbb0002", "email"), ("acme_lead", "cccc0003", "knowledge")}
    assert all(r["obs_id"] for r in rows)
    assert len(vault.observe_bodies) == 3 and vault.observe_bodies[0]["task"] == "t_learn"
    assert "pending aaaa0001" in vault.observe_bodies[0]["lesson"]
    # Still only pending in the vault; approved skill bytes untouched.
    assert vault.doctrine == {}
    assert not (root / "skills" / "devops" / "ops-thing").exists()
    # Idempotent.
    assert sb.stage_pending_to_vault() == []
    assert len(vault.observe_bodies) == 3
    dry = sb.stage_pending_to_vault(dry_run=True)
    assert dry == []
    # A bridge-staged projection (summary prefix) is never mirrored back as learning.
    p = _stage_record(root, "dddd0004", "skill-a", "devops")
    rec = json.loads(p.read_text())
    rec["summary"] = sb.PROJECTION_SUMMARY_PREFIX + "obs_x (verification) into skill-a"
    p.write_text(json.dumps(rec), encoding="utf-8")
    assert sb.stage_pending_to_vault() == [] and len(vault.observe_bodies) == 3


def test_materialize_refuses_when_gate_off_and_stages_when_on(vault, bridge_env, monkeypatch):
    root = bridge_env["root"]
    r = sb.provision_skill("skill-a", "acme_lead")
    with pytest.raises(sb.VaultError, match="write_approval off"):
        sb.materialize_lesson(home_label="acme_lead", skill="skill-a", capability="verification",
                              obs_id="obs_test9", lesson="L")
    (root / "profiles" / "acme_lead" / "config.yaml").write_text("skills:\n  write_approval: true\n", encoding="utf-8")
    out = sb.materialize_lesson(home_label="acme_lead", skill="skill-a", capability="verification",
                                obs_id="obs_test9", lesson="L")
    assert out["staged"] is True
    rec = json.loads((root / "profiles" / "acme_lead" / "pending" / "skills" / f"{out['pending_id']}.json").read_text())
    assert rec["payload"]["action"] == "write_file" and rec["payload"]["file_path"] == sb.MATERIALIZE_REL_PATH
    assert "obs_test9" in rec["payload"]["file_content"]
    # Nothing written into the skill; approved bytes unchanged.
    assert not (Path(r.target_path) / sb.MATERIALIZE_REL_PATH).exists()
    assert sb.tree_hash(r.target_path) == r.sha256_tree
    assert sb.materialize_lesson(home_label="acme_lead", skill="skill-a", capability="verification",
                                 obs_id="obs_test9", lesson="L")["staged"] is True  # second staging also pending-only
    # Unknown home / skill fail loudly.
    with pytest.raises(sb.VaultError):
        sb.materialize_lesson(home_label="nope", skill="skill-a", capability="verification", obs_id="obs_1", lesson="L")


def test_cli_preflight_exit_codes(bridge_env, capsys):
    assert sb.main(["preflight", "--profile", "acme_lead", "--skill", "skill-a", "--task", "t_cli"]) == 0
    assert sb.main(["--json", "preflight", "--profile", "acme_lead", "--skill", "unreg"]) == 2
    captured = capsys.readouterr()
    assert "REFUSED" in captured.err and sb.REFUSED_UNREGISTERED in captured.out
    assert sb.main(["drift"]) == 0
    assert sb.main(["unprovision", "--profile", "acme_lead", "--skill", "skill-a"]) == 0
    assert sb.main(["unprovision", "--profile", "acme_lead", "--skill", "skill-a"]) == 2


def test_cli_materialize_requires_a_promoted_lesson(vault, bridge_env, monkeypatch, capsys):
    monkeypatch.setenv("SKILL_VERIFIER_TOKEN", vault.token)
    sb.provision_skill("skill-a", "acme_lead")
    (bridge_env["root"] / "profiles" / "acme_lead" / "config.yaml").write_text("skills:\n  write_approval: true\n", encoding="utf-8")
    oid = sb.vault_observe("verification", "Projected lesson.")["obs_id"]
    # Pending (not promoted) -> nothing to project.
    assert sb.main(["materialize", "--capability", "verification", "--obs-id", oid, "--skill", "skill-a",
                    "--home", "acme_lead"]) == 1
    assert "only promoted" in capsys.readouterr().err
    sb.vault_promote("verification", oid, verifier="erika", attested_by="erika", attestation_evidence="reviewed")
    assert sb.main(["materialize", "--capability", "verification", "--obs-id", oid, "--skill", "skill-a",
                    "--home", "acme_lead"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["staged"] is True and out["frozen_target"] is False
    rec = json.loads((bridge_env["root"] / "profiles" / "acme_lead" / "pending" / "skills" / f"{out['pending_id']}.json").read_text())
    assert "Projected lesson." in rec["payload"]["file_content"] and "FROZEN" not in rec["summary"]
