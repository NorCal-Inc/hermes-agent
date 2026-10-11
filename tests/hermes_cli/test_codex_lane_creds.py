"""Tests for hermes_cli.codex_lane_creds (C1.2d) and the sandbox no-refresh switch.

Everything runs against a temp HERMES_HOME with FAKE tokens; the refresh
endpoint is a stub (never the network, never the real auth store).
"""

from __future__ import annotations

import base64
import json
import os
import stat
import time
from pathlib import Path

import pytest

from hermes_cli import auth as auth_mod
from hermes_cli import codex_lane_creds as clc


def _jwt(exp_epoch: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(exp_epoch)}).encode()).rstrip(b"=").decode()
    return f"h.{payload}.s"


def _root_store(access: str, refresh: str = "FAKE_REFRESH_0") -> dict:
    return {
        "version": 1,
        "active_provider": "openai-codex",
        "providers": {
            "openai-codex": {"tokens": {"access_token": access, "refresh_token": refresh},
                             "last_refresh": "2026-10-11T00:00:00Z", "auth_mode": "chatgpt"},
            "anthropic": {"api_key": "FAKE_ANTHROPIC"},
            "openrouter": {"api_key": "FAKE_OPENROUTER"},
            "xai": {"tokens": {"access_token": "FAKE_XAI"}},
        },
        "credential_pool": {"openai-codex": [{"source": "device_code", "access_token": access,
                                              "refresh_token": refresh}]},
    }


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Temp root <tmp>/hermes with a profile HERMES_HOME so the global store is distinct."""
    hermes_root = tmp_path / "hermes"
    profile = hermes_root / "profiles" / "lane_lead"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.delenv("HERMES_SANDBOX_NO_REFRESH", raising=False)
    root_path = hermes_root / "auth.json"
    root_path.write_text(json.dumps(_root_store(_jwt(time.time() + 3600))), encoding="utf-8")
    root_path.chmod(0o600)
    return {"root": root_path, "profile": profile, "creds": tmp_path / "creds"}


class _Stub:
    def __init__(self):
        self.calls = 0

    def __call__(self, access: str, refresh: str) -> dict:
        self.calls += 1
        assert access.startswith("h.") and refresh.startswith("FAKE_")
        return {"access_token": _jwt(time.time() + 7200), "refresh_token": "FAKE_REFRESH_1",
                "last_refresh": "2026-10-11T01:00:00Z"}


# ---------------------------------------------------------------- (1a) refresher

def test_refresher_does_not_refresh_outside_skew(root):
    stub = _Stub()
    before = root["root"].read_bytes()
    assert clc.refresh_root_codex_if_expiring(root["root"], skew_seconds=900, refresh_fn=stub) == "fresh"
    assert stub.calls == 0
    assert root["root"].read_bytes() == before


def test_refresher_refreshes_inside_skew_and_writes_root_only(root):
    root["root"].write_text(json.dumps(_root_store(_jwt(time.time() + 300))), encoding="utf-8")
    stub = _Stub()
    assert clc.refresh_root_codex_if_expiring(root["root"], skew_seconds=900, refresh_fn=stub) == "refreshed"
    assert stub.calls == 1
    store = json.loads(root["root"].read_text(encoding="utf-8"))
    tok = store["providers"]["openai-codex"]["tokens"]
    assert tok["refresh_token"] == "FAKE_REFRESH_1"
    assert not auth_mod._codex_access_token_is_expiring(tok["access_token"], 900)
    assert store["providers"]["anthropic"] == {"api_key": "FAKE_ANTHROPIC"}  # untouched
    assert store["credential_pool"]["openai-codex"][0]["refresh_token"] == "FAKE_REFRESH_1"  # pool alias synced
    assert stat.S_IMODE(root["root"].stat().st_mode) == 0o600
    assert not (root["profile"] / "auth.json").exists()  # never the profile store
    assert not list(root["root"].parent.glob("auth.json.tmp.*"))


def test_refresher_reports_absent_without_writing(root):
    root["root"].write_text(json.dumps({"version": 1, "providers": {"anthropic": {"api_key": "FAKE"}}}))
    before = root["root"].read_bytes()
    stub = _Stub()
    assert clc.refresh_root_codex_if_expiring(root["root"], refresh_fn=stub) == "absent"
    assert stub.calls == 0 and root["root"].read_bytes() == before


# ---------------------------------------------------------------- (1b) trimmed writer

def test_trimmed_file_contains_only_openai_codex(root):
    store = json.loads(root["root"].read_text(encoding="utf-8"))
    p = clc.materialize_lane_file("logos", store, creds_root=root["creds"])
    assert p == root["creds"] / "logos" / "auth.json"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert set(doc["providers"]) == {"openai-codex"}
    assert doc["active_provider"] == "openai-codex"
    assert "credential_pool" not in doc
    text = p.read_text(encoding="utf-8")
    for leaked in ("FAKE_ANTHROPIC", "FAKE_OPENROUTER", "FAKE_XAI", "anthropic", "openrouter", "xai"):
        assert leaked not in text
    assert doc["providers"]["openai-codex"]["tokens"]["refresh_token"] == "FAKE_REFRESH_0"


def test_trimmed_file_updates_in_place_on_same_inode_and_skips_when_unchanged(root):
    store = json.loads(root["root"].read_text(encoding="utf-8"))
    p = clc.materialize_lane_file("logos", store, creds_root=root["creds"])
    ino, mtime = p.stat().st_ino, p.stat().st_mtime_ns
    clc.materialize_lane_file("logos", store, creds_root=root["creds"])
    assert p.stat().st_ino == ino and p.stat().st_mtime_ns == mtime  # unchanged -> not rewritten
    store["providers"]["openai-codex"]["tokens"]["refresh_token"] = "FAKE_REFRESH_1"
    clc.materialize_lane_file("logos", store, creds_root=root["creds"])
    assert p.stat().st_ino == ino  # bwrap binds the inode: must not be replaced
    assert json.loads(p.read_text())["providers"]["openai-codex"]["tokens"]["refresh_token"] == "FAKE_REFRESH_1"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_trimmed_writer_refuses_symlink_and_loose_modes(root, tmp_path):
    store = json.loads(root["root"].read_text(encoding="utf-8"))
    lane_dir = root["creds"] / "evil"
    lane_dir.mkdir(parents=True)
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    (lane_dir / "auth.json").symlink_to(target)
    with pytest.raises(clc.CodexLaneCredsError, match="symlink"):
        clc.materialize_lane_file("evil", store, creds_root=root["creds"])
    p = clc.materialize_lane_file("loose", store, creds_root=root["creds"])
    p.chmod(0o644)
    with pytest.raises(clc.CodexLaneCredsError, match="group/other"):
        clc.materialize_lane_file("loose", store, creds_root=root["creds"])
    with pytest.raises(clc.CodexLaneCredsError):
        clc.lane_creds_path("../x", creds_root=root["creds"])


def test_run_once_refreshes_then_regenerates_every_codex_lane(root):
    root["root"].write_text(json.dumps(_root_store(_jwt(time.time() + 300))), encoding="utf-8")
    manifest = {"lanes": {"logos": {}, "ncass": {"llm_source": "codex"}, "orion": {"llm_source": "claude_code"}}}
    stub = _Stub()
    res = clc.run_once(manifest=manifest, root_path=root["root"], creds_root=root["creds"],
                       home=root["root"].parent.parent, refresh_fn=stub, skew_seconds=900)
    assert res["refresh"] == "refreshed" and stub.calls == 1
    assert res["lanes"] == {"logos": "ok", "ncass": "ok"}  # claude_code lane gets no file
    assert not (root["creds"] / "orion").exists()
    for lane in ("logos", "ncass"):
        doc = json.loads((root["creds"] / lane / "auth.json").read_text())
        assert set(doc["providers"]) == {"openai-codex"}
        assert doc["providers"]["openai-codex"]["tokens"]["refresh_token"] == "FAKE_REFRESH_1"


def test_run_once_is_noop_when_sandbox_not_enforced(root, monkeypatch):
    from hermes_cli import worker_sandbox as ws
    monkeypatch.setattr(ws, "sandbox_config", lambda cfg=None: {"mode": "off", "lanes": []})
    assert clc.run_once(root_path=root["root"], creds_root=root["creds"]) == {"refresh": "skipped", "lanes": {}}
    assert not root["creds"].exists()


# ---------------------------------------------------------------- (2) worker-side switch

def _profile_store(profile: Path, access: str, refresh: str = "FAKE_REFRESH_P") -> None:
    (profile / "auth.json").write_text(json.dumps({
        "version": 1, "active_provider": "openai-codex",
        "providers": {"openai-codex": {"tokens": {"access_token": access, "refresh_token": refresh}}}}))


def test_worker_with_flag_never_calls_refresh_endpoint(root, monkeypatch):
    monkeypatch.setenv("HERMES_SANDBOX_NO_REFRESH", "1")
    calls = []
    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", lambda *a, **k: calls.append(1) or {})
    monkeypatch.setattr(auth_mod, "_recover_codex_tokens_from_cli", lambda reason: calls.append(1) or None)
    # inside the normal skew window (120s) -> an unsandboxed worker would refresh; this one must not
    near = _jwt(time.time() + 60)
    _profile_store(root["profile"], near)
    creds = auth_mod.resolve_codex_runtime_credentials()
    assert creds["api_key"] == near and creds["source"] == "hermes-auth-store"
    assert calls == []
    # force_refresh is ignored too
    creds = auth_mod.resolve_codex_runtime_credentials(force_refresh=True)
    assert creds["api_key"] == near and calls == []
    # expired -> clear, attributed error; still no refresh, still no store write
    before = (root["profile"] / "auth.json").read_bytes()
    _profile_store(root["profile"], _jwt(time.time() - 5))
    with pytest.raises(auth_mod.AuthError) as exc:
        auth_mod.resolve_codex_runtime_credentials()
    assert exc.value.code == auth_mod.CODEX_REFRESH_OWNED_BY_GATEWAY_CODE
    assert exc.value.relogin_required is False
    assert "owned by the gateway" in str(exc.value)
    assert calls == []
    with pytest.raises(auth_mod.AuthError):
        auth_mod._refresh_codex_auth_tokens({"access_token": "h.x.s", "refresh_token": "FAKE"}, 5.0)
    assert calls == []
    assert (root["profile"] / "auth.json").read_bytes() != before  # only our own test write


def test_worker_without_flag_still_refreshes(root, monkeypatch):
    stub = _Stub()
    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", lambda a, r, timeout_seconds=0: stub(a, r))
    _profile_store(root["profile"], _jwt(time.time() + 60))
    creds = auth_mod.resolve_codex_runtime_credentials()
    assert stub.calls == 1
    assert not auth_mod._codex_access_token_is_expiring(creds["api_key"], 120)
