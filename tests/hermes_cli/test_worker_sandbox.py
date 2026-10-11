"""Tests for hermes_cli.worker_sandbox (bwrap argv builder, C1.1)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import worker_sandbox as ws

HOME = Path("/home/chris")
FORBIDDEN_FRAGMENTS = [
    "/.hermes/secrets",
    "/.ssh",
    "op_cache.json",
    "docker.sock",
    "/run/user",
    "/.config/op",
]


def _manifest(tmp_path: Path, **overrides) -> dict:
    m = {
        "version": 1,
        "sandbox_home_root": str(tmp_path / "homes"),
        "shared_ro": [str(HOME / ".hermes" / "doctrine")],
        "credentials_ro": [],
        "groups": {},
        "lanes": {
            "logos": {
                "repos": ["/home/chris/hermes/repos/Logos-Covenant"],
                "vault": ["/home/chris/services/life-wiki/vault/Business/Companies/Logos-Covenant-Inc"],
            },
            "ncass": {
                "repos": ["/home/chris/hermes/repos/NCASS-INC"],
                "vault": ["/home/chris/services/life-wiki/vault/Business/Companies/NCASS"],
            },
            "triptracker": {"repos": ["/home/chris/TripTrackertechnologies"], "vault": []},
        },
    }
    m.update(overrides)
    return m


def _build(tmp_path: Path, lane="logos", **kw) -> list[str]:
    return ws.build_bwrap_argv(
        lane,
        "logos_covenant_lead",
        HOME / ".hermes" / "profiles" / "logos_covenant_lead",
        tmp_path / "ws",
        kw.pop("manifest", None) or _manifest(tmp_path),
        kanban_db_dir=kw.pop("kanban_db_dir", str(tmp_path / "kanban")),
        home=HOME,
        exists=kw.pop("exists", lambda p: True),
        **kw,
    )


def _binds(argv: list[str], flag: str) -> list[tuple[str, str]]:
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag]


# ---------------------------------------------------------------- argv shape

def test_argv_has_unshare_flags_and_die_with_parent(tmp_path):
    argv = _build(tmp_path)
    assert argv[0] == "bwrap"
    for flag in ("--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
                 "--unshare-cgroup-try", "--share-net", "--die-with-parent", "--new-session"):
        assert flag in argv
    assert ["--dev", "/dev"] == argv[argv.index("--dev"):argv.index("--dev") + 2]
    assert ["--proc", "/proc"] == argv[argv.index("--proc"):argv.index("--proc") + 2]
    assert ("--tmpfs", "/tmp") in zip(argv, argv[1:])
    assert ("--tmpfs", "/run") in zip(argv, argv[1:])
    assert ("--tmpfs", str(HOME)) in zip(argv, argv[1:])
    assert ("--setenv", "HERMES_ENV_SOURCED") in zip(argv, argv[1:])
    assert argv[-2:] == ["--chdir", str(tmp_path / "ws")]
    assert "--" not in argv


def test_home_tmpfs_precedes_home_rebinds(tmp_path):
    argv = _build(tmp_path)
    home_tmpfs = argv.index(str(HOME)) - 1
    assert argv[home_tmpfs] == "--tmpfs"
    first_home_bind = min(
        i for i, a in enumerate(argv) if a in ("--bind", "--ro-bind") and argv[i + 1].startswith(str(HOME) + "/")
    )
    assert home_tmpfs < first_home_bind


def test_system_dirs_only_when_present(tmp_path):
    argv = _build(tmp_path, exists=lambda p: p != "/lib64")
    srcs = [s for s, _ in _binds(argv, "--ro-bind")]
    assert "/usr" in srcs and "/etc" in srcs and "/lib64" not in srcs


def test_never_binds_forbidden_paths(tmp_path):
    argv = _build(tmp_path)
    joined = "\n".join(argv)
    for frag in FORBIDDEN_FRAGMENTS:
        assert frag not in joined, frag
    assert str(HOME / ".claude") not in joined
    assert str(HOME / ".codex") not in joined


@pytest.mark.parametrize("bad", [
    "/home/chris/.hermes/secrets",
    "/home/chris/.hermes/secrets/Orion.env",
    "/home/chris/.ssh",
    "/home/chris/.config/op",
    "/home/chris/.hermes/cache/op_cache.json",
    "/var/run/docker.sock",
    "/run/user/1000",
    "/home/chris/.claude",
    "/home/chris/.codex",
])
def test_manifest_cannot_smuggle_forbidden_paths(tmp_path, bad):
    m = _manifest(tmp_path)
    m["lanes"]["logos"]["extra_rw"] = [bad]
    with pytest.raises(ws.SandboxConfigError):
        _build(tmp_path, manifest=m)
    m = _manifest(tmp_path, shared_ro=[bad])
    with pytest.raises(ws.SandboxConfigError):
        _build(tmp_path, manifest=m)


def test_profile_home_must_not_be_hermes_root(tmp_path):
    with pytest.raises(ws.SandboxConfigError):
        ws.build_bwrap_argv("logos", "x", HOME / ".hermes", tmp_path, _manifest(tmp_path), home=HOME,
                            exists=lambda p: True)


# ---------------------------------------------------------------- credentials

def test_credential_files_bound_ro_only_when_listed(tmp_path):
    cred = str(HOME / ".claude" / ".credentials.json")
    auth = str(HOME / ".hermes" / "auth.json")
    without = _build(tmp_path)
    assert cred not in without and auth not in without
    with_creds = _build(tmp_path, manifest=_manifest(tmp_path, credentials_ro=[cred, auth]))
    ro = _binds(with_creds, "--ro-bind")
    rw = _binds(with_creds, "--bind")
    assert (cred, cred) in ro and (auth, auth) in ro
    assert all(s not in (cred, auth) for s, _ in rw)


def test_credential_entry_may_not_be_directory_or_nested(tmp_path):
    with pytest.raises(ws.SandboxConfigError):
        _build(tmp_path, manifest=_manifest(tmp_path, credentials_ro=[str(HOME / ".claude")]))
    with pytest.raises(ws.SandboxConfigError):
        _build(tmp_path, manifest=_manifest(tmp_path, credentials_ro=[str(HOME / ".claude" / "sub" / "x.json")]))
    with pytest.raises(ws.SandboxConfigError):
        _build(tmp_path, manifest=_manifest(tmp_path, credentials_ro=[str(HOME / ".hermes" / "secrets" / "x.env")]))
    with pytest.raises(ws.SandboxConfigError):
        _build(tmp_path, manifest=_manifest(tmp_path, credentials_ro=[str(HOME / ".ssh" / "id_ed25519")]))


# ---------------------------------------------------------------- lane isolation

def test_rw_binds_profile_workspace_kanban_dir_and_lane_home(tmp_path):
    argv = _build(tmp_path)
    rw = [s for s, _ in _binds(argv, "--bind")]
    assert str(HOME / ".hermes" / "profiles" / "logos_covenant_lead") in rw
    assert str(tmp_path / "ws") in rw
    assert str(tmp_path / "kanban") in rw
    lane_home = str(tmp_path / "homes" / "logos")
    assert lane_home in rw
    assert ["--setenv", "HOME", lane_home] == argv[argv.index("HOME") - 1:argv.index("HOME") + 2]


def test_lane_never_sees_other_lane_repos_or_vault(tmp_path):
    m = _manifest(tmp_path)
    logos = "\n".join(_build(tmp_path, "logos", manifest=m))
    ncass = "\n".join(_build(tmp_path, "ncass", manifest=m))
    for p in m["lanes"]["ncass"]["repos"] + m["lanes"]["ncass"]["vault"]:
        assert p not in logos
    for p in m["lanes"]["logos"]["repos"] + m["lanes"]["logos"]["vault"]:
        assert p not in ncass
    # triptracker is not grouped with logos in this manifest
    assert "/home/chris/TripTrackertechnologies" not in logos


def test_lane_group_shares_repos_and_vault(tmp_path):
    m = _manifest(tmp_path, groups={"logos_triptracker": ["logos", "triptracker"]})
    logos = "\n".join(_build(tmp_path, "logos", manifest=m))
    trip = "\n".join(_build(tmp_path, "triptracker", manifest=m))
    assert "/home/chris/TripTrackertechnologies" in logos
    assert "/home/chris/hermes/repos/Logos-Covenant" in trip
    assert "/home/chris/hermes/repos/NCASS-INC" not in logos
    assert "/home/chris/hermes/repos/NCASS-INC" not in trip


def test_unknown_lane_rejected(tmp_path):
    with pytest.raises(ws.SandboxConfigError):
        _build(tmp_path, "glasspepper")


def test_example_manifest_parses_and_builds_every_lane():
    import yaml
    p = Path(__file__).resolve().parents[2] / "docs" / "worker-sandbox" / "lanes.example.yaml"
    m = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert set(m["lanes"]) == {"logos", "triptracker", "ncass", "northcaledonia", "orion", "glasspepper"}
    joined = "\n".join(str(v) for v in m.values())
    for side in ("BobtailBoss", "CDL-Prep-Helper", "Route-Plan-Go", "Grants-101"):
        assert side not in joined
    for lane in m["lanes"]:
        argv = ws.build_bwrap_argv(lane, "p", HOME / ".hermes" / "profiles" / "p", "/tmp/ws", m,
                                   home=HOME, exists=lambda p: True)
        assert "--die-with-parent" in argv
    logos = "\n".join(ws.build_bwrap_argv("logos", "p", HOME / ".hermes" / "profiles" / "p", "/tmp/ws", m,
                                          home=HOME, exists=lambda p: True))
    assert "Logos-Investment-Group" in logos and "iKBR-Active-Trader" in logos
    assert "TripTrackertechnologies" in logos and "NCASS-INC" not in logos


# ---------------------------------------------------------------- wrap() decisions

CMD = ["hermes", "-p", "logos_covenant_lead", "chat", "-q", "x"]
ENFORCE = {"worker_sandbox": {"mode": "enforce", "lanes": ["logos"]}}


def _wrap(tmp_path, *, cfg=ENFORCE, lane="logos", executor_lane=None, manifest="default", profile="logos_covenant_lead",
          **kw):
    if manifest == "default":
        manifest = _manifest(tmp_path)
    task = SimpleNamespace(id="t_x", assignee=profile, executor_lane=executor_lane)
    return ws.wrap(
        CMD, task=task, profile=profile, profile_home=HOME / ".hermes" / "profiles" / profile,
        workspace=tmp_path / "ws", env={"HERMES_KANBAN_DB": str(tmp_path / "kanban" / "kanban.db")},
        cfg=cfg, manifest=manifest, **kw,
    )


def test_wrap_enforced_lane_gets_bwrap_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "lane_for", lambda p: "logos")
    out = _wrap(tmp_path)
    assert out[0] == "bwrap"
    assert out[-len(CMD):] == CMD
    assert out[-len(CMD) - 1] == "--"
    assert str(tmp_path / "kanban") in out  # DB directory, not the file
    assert str(tmp_path / "kanban" / "kanban.db") not in out


def test_wrap_mode_off_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "lane_for", lambda p: "logos")
    assert _wrap(tmp_path, cfg={}) == CMD
    assert _wrap(tmp_path, cfg={"worker_sandbox": {"mode": "off", "lanes": ["logos"]}}) == CMD


@pytest.mark.parametrize("lane", ["claude", "codex_verify", "claude_recovery"])
def test_wrap_executor_lanes_unchanged(tmp_path, monkeypatch, lane):
    monkeypatch.setattr(ws, "lane_for", lambda p: "logos")
    assert _wrap(tmp_path, executor_lane=lane) == CMD


def test_wrap_shared_or_untagged_profile_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "lane_for", lambda p: None)
    assert _wrap(tmp_path) == CMD


def test_wrap_non_opted_lane_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "lane_for", lambda p: "ncass")
    assert _wrap(tmp_path) == CMD


def test_wrap_missing_manifest_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "lane_for", lambda p: "logos")
    assert _wrap(tmp_path, manifest=None, manifest_path=tmp_path / "nope.yaml") == CMD


def test_wrap_enforced_lane_absent_from_manifest_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "lane_for", lambda p: "logos")
    m = _manifest(tmp_path)
    del m["lanes"]["logos"]
    with pytest.raises(ws.SandboxConfigError):
        _wrap(tmp_path, manifest=m)


def test_lane_for_uses_profile_company(monkeypatch):
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_company", lambda n: {"a": "logos", "b": "shared", "c": None}[n])
    assert ws.lane_for("a") == "logos"
    assert ws.lane_for("b") is None
    assert ws.lane_for("c") is None
    assert ws.lane_for("") is None


def test_load_manifest_missing_returns_none_and_bad_raises(tmp_path):
    assert ws.load_manifest(tmp_path / "absent.yaml") is None
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ws.SandboxConfigError):
        ws.load_manifest(bad)
    good = tmp_path / "good.yaml"
    good.write_text("lanes:\n  logos:\n    repos: []\n", encoding="utf-8")
    assert ws.load_manifest(good)["lanes"]["logos"] == {"repos": []}


def test_sandbox_config_rejects_unknown_mode():
    with pytest.raises(ws.SandboxConfigError):
        ws.sandbox_config({"worker_sandbox": {"mode": "audit"}})
    assert ws.sandbox_config({}) == {"mode": "off", "lanes": []}
    assert ws.sandbox_config({"worker_sandbox": {"mode": "enforce", "lanes": "Logos"}}) == {
        "mode": "enforce", "lanes": ["logos"]}


# ---------------------------------------------------------------- real bwrap (optional)

@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed")
@pytest.mark.skipif(Path.home() != HOME, reason="test assumes the hermes host home")
def test_bwrap_hides_secrets_and_home(tmp_path):
    for d in ("ws", "kanban", "profile", "homes/logos"):
        (tmp_path / d).mkdir(parents=True)
    m = _manifest(tmp_path, shared_ro=[], lanes={"logos": {"repos": [], "vault": []}})
    argv = ws.build_bwrap_argv("logos", "p", tmp_path / "profile", tmp_path / "ws", m,
                               kanban_db_dir=tmp_path / "kanban", home=HOME)
    probe = (
        "test ! -e /home/chris/.hermes/secrets/Orion.env && test ! -e /home/chris/.hermes/secrets"
        " && test ! -e /home/chris/.ssh && test ! -e /home/chris/.hermes/cache/op_cache.json"
        " && test ! -e /var/run/docker.sock && test ! -e /run/user/$(id -u)"
        " && test \"$HOME\" = \"%s\" && test \"$HERMES_ENV_SOURCED\" = 1"
        " && test \"$PWD\" = \"%s\" && touch \"%s/ok\""
    ) % (tmp_path / "homes" / "logos", tmp_path / "ws", tmp_path / "ws")
    res = subprocess.run(argv + ["--", "/bin/sh", "-c", probe], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "ws" / "ok").exists()
