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
    trimmed = ws.lane_creds_file("logos", HOME)
    m = _manifest(tmp_path)
    m["lanes"]["logos"]["llm_source"] = "claude_code"
    without = _build(tmp_path, manifest=m)
    assert cred not in without and auth not in without
    m["credentials_ro"] = [cred, auth]
    with_creds = _build(tmp_path, manifest=m)
    ro = _binds(with_creds, "--ro-bind")
    rw = _binds(with_creds, "--bind")
    assert (cred, cred) in ro
    # C1.2d: the root store is never bound as-is, even when listed.
    assert (auth, auth) not in ro and auth not in with_creds
    assert all(s not in (cred, auth, trimmed) for s, _ in rw)


# ---------------------------------------------------------------- C1.2d trimmed codex store

def test_codex_lane_binds_trimmed_file_at_root_auth_path_and_sets_no_refresh(tmp_path):
    auth = str(HOME / ".hermes" / "auth.json")
    trimmed = ws.lane_creds_file("logos", HOME)
    assert trimmed == str(HOME / ".hermes" / "security" / "worker-sandbox" / "creds" / "logos" / "auth.json")
    m = _manifest(tmp_path, credentials_ro=[auth])  # default llm_source: codex
    argv = _build(tmp_path, manifest=m)
    ro = _binds(argv, "--ro-bind")
    assert (trimmed, auth) in ro
    assert (auth, auth) not in ro
    assert all(s != auth for s, _ in ro)  # the real root store is never a bind source
    assert ("--setenv", "HERMES_SANDBOX_NO_REFRESH") in zip(argv, argv[1:])
    assert argv[argv.index("HERMES_SANDBOX_NO_REFRESH") + 1] == "1"
    # the trimmed file is bound even when credentials_ro does not list the root store
    argv2 = _build(tmp_path, manifest=_manifest(tmp_path, credentials_ro=[]))
    assert (trimmed, auth) in _binds(argv2, "--ro-bind")


def test_codex_lane_missing_trimmed_file_fails_closed(tmp_path):
    trimmed = ws.lane_creds_file("logos", HOME)
    with pytest.raises(ws.SandboxConfigError, match="codex-sourced"):
        _build(tmp_path, exists=lambda p: p != trimmed)
    # explicit llm_source: codex behaves the same as the default
    m = _manifest(tmp_path)
    m["lanes"]["logos"]["llm_source"] = "codex"
    with pytest.raises(ws.SandboxConfigError, match="codex-sourced"):
        _build(tmp_path, manifest=m, exists=lambda p: p != trimmed)


def test_claude_code_lane_gets_no_root_store_view(tmp_path):
    auth = str(HOME / ".hermes" / "auth.json")
    cred = str(HOME / ".claude" / ".credentials.json")
    m = _manifest(tmp_path, credentials_ro=[cred, auth])
    m["lanes"]["logos"]["llm_source"] = "claude_code"
    argv = _build(tmp_path, manifest=m, exists=lambda p: p != ws.lane_creds_file("logos", HOME))
    assert auth not in argv and ws.lane_creds_file("logos", HOME) not in argv
    assert (cred, cred) in _binds(argv, "--ro-bind")
    assert argv[argv.index("HERMES_SANDBOX_NO_REFRESH") + 1] == "1"


def test_unknown_llm_source_rejected(tmp_path):
    m = _manifest(tmp_path)
    m["lanes"]["logos"]["llm_source"] = "openrouter"
    with pytest.raises(ws.SandboxConfigError, match="llm_source"):
        _build(tmp_path, manifest=m)


def test_wrap_codex_lane_binds_trimmed_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "lane_for", lambda p: "logos")
    trimmed = Path(ws.lane_creds_file("logos", Path.home()))
    monkeypatch.setattr(ws, "lane_creds_file", lambda lane, home: str(tmp_path / "creds" / lane / "auth.json"))
    with pytest.raises(ws.SandboxConfigError):
        _wrap(tmp_path)  # file not materialised yet -> no unsandboxed spawn, no spawn at all
    f = tmp_path / "creds" / "logos" / "auth.json"
    f.parent.mkdir(parents=True)
    f.write_text("{}", encoding="utf-8")
    out = _wrap(tmp_path)
    assert out[0] == "bwrap"
    assert (str(f), str(Path.home() / ".hermes" / "auth.json")) in _binds(out, "--ro-bind")
    assert str(trimmed) not in out


def test_credential_entry_may_not_be_directory_or_nested(tmp_path):
    with pytest.raises(ws.SandboxConfigError):
        _build(tmp_path, manifest=_manifest(tmp_path, credentials_ro=[str(HOME / ".claude")]))
    with pytest.raises(ws.SandboxConfigError):
        _build(tmp_path, manifest=_manifest(tmp_path, credentials_ro=[str(HOME / ".claude" / "sub" / "x.json")]))
    with pytest.raises(ws.SandboxConfigError):
        _build(tmp_path, manifest=_manifest(tmp_path, credentials_ro=[str(HOME / ".hermes" / "secrets" / "x.env")]))
    with pytest.raises(ws.SandboxConfigError):
        _build(tmp_path, manifest=_manifest(tmp_path, credentials_ro=[str(HOME / ".ssh" / "id_ed25519")]))


# ---------------------------------------------------------------- skill roots (C1.2c)

SKILL_ROOTS = [str(HOME / ".claude" / "plugins"), str(HOME / ".claude" / "settings.json"), str(HOME / ".agents")]


def test_skill_roots_bound_ro_only_when_listed(tmp_path):
    without = "\n".join(_build(tmp_path))
    for p in SKILL_ROOTS:
        assert p not in without
    argv = _build(tmp_path, manifest=_manifest(tmp_path, skill_roots_ro=SKILL_ROOTS))
    ro = _binds(argv, "--ro-bind")
    rw = _binds(argv, "--bind")
    for p in SKILL_ROOTS:
        assert (p, p) in ro
        assert all(s != p for s, _ in rw)
    # the roots do not drag the rest of ~/.claude or ~/.codex in
    joined = "\n".join(argv)
    assert str(HOME / ".claude" / ".credentials.json") not in joined
    assert str(HOME / ".claude.json") not in joined
    assert str(HOME / ".codex") not in joined
    # ro-bound after the $HOME tmpfs so they survive the overlay
    home_tmpfs = argv.index(str(HOME)) - 1
    assert all(argv.index(p) > home_tmpfs for p in SKILL_ROOTS)


def test_skill_roots_absent_or_none_means_no_binds(tmp_path):
    for m in (_manifest(tmp_path), _manifest(tmp_path, skill_roots_ro=None), _manifest(tmp_path, skill_roots_ro=[])):
        joined = "\n".join(_build(tmp_path, manifest=m))
        assert all(p not in joined for p in SKILL_ROOTS)


@pytest.mark.parametrize("bad", [
    "/home/chris/.claude/plugins",   # scalar string: NOT a one-element list
    "",
    {},
    {"a": "/home/chris/.claude/plugins"},
    0,
    1,
    False,
    True,
    3.5,
    [1],
    [None],
    [True],
    [""],
    ["/home/chris/.claude/plugins", 1],
    [["/home/chris/.claude/plugins"]],
    [{"path": "/home/chris/.claude/plugins"}],
    ("/home/chris/.claude/plugins",),  # tuple is not a list either
    ["/home/chris/.claude/.credentials.json"],
])
def test_skill_roots_rejects_malformed_values_fail_closed(tmp_path, bad):
    """Verifier t_6d1df5a4 finding: a scalar was accepted and {}, 0, False were treated as []."""
    with pytest.raises(ws.SandboxConfigError):
        _build(tmp_path, manifest=_manifest(tmp_path, skill_roots_ro=bad))
    with pytest.raises(ws.SandboxConfigError):
        ws.probe_checks("logos", "p", HOME / ".hermes" / "profiles" / "p", tmp_path / "ws",
                        _manifest(tmp_path, skill_roots_ro=bad), home=HOME, uid=1000,
                        exists=lambda p: True, listdir=lambda d: [])


@pytest.mark.parametrize("bad", [
    "/home/chris/.claude/.credentials.json",
    "/home/chris/.claude.json",
    "/home/chris/.claude/projects",
    "/home/chris/.claude",
    "/home/chris/.codex",
    "/home/chris/.codex/auth.json",
    "/home/chris/.claude/plugins/../.credentials.json",   # traversal
    "/home/chris/.claude/plugins/..",
    "/home/chris/.claude/plugins/some-plugin",            # nested under an allowed root
    "/home/chris/.agents/skills",
    "/home/chris/.hermes/secrets",
    "/home/chris/.ssh",
    "/home/chris/.hermes/skills",                         # legitimate elsewhere, not via this key
])
def test_skill_roots_rejects_everything_outside_the_allow_list(tmp_path, bad):
    with pytest.raises(ws.SandboxConfigError):
        _build(tmp_path, manifest=_manifest(tmp_path, skill_roots_ro=[bad]))
    # one bad entry poisons the whole build even alongside good ones
    with pytest.raises(ws.SandboxConfigError):
        _build(tmp_path, manifest=_manifest(tmp_path, skill_roots_ro=SKILL_ROOTS + [bad]))


def test_skill_roots_do_not_loosen_other_keys(tmp_path):
    # ~/.claude stays forbidden for every other key (~/.agents was never forbidden)
    for p in SKILL_ROOTS[:2]:
        with pytest.raises(ws.SandboxConfigError):
            _build(tmp_path, manifest=_manifest(tmp_path, shared_ro=[p]))
        m = _manifest(tmp_path)
        m["lanes"]["logos"]["extra_ro"] = [p]
        with pytest.raises(ws.SandboxConfigError):
            _build(tmp_path, manifest=m)
    with pytest.raises(ws.SandboxConfigError):
        _build(tmp_path, manifest=_manifest(tmp_path, credentials_ro=[str(HOME / ".claude" / "plugins")]))
    # the credential file is still admitted ONLY through credentials_ro, unchanged
    argv = _build(tmp_path, manifest=_manifest(
        tmp_path, skill_roots_ro=SKILL_ROOTS, credentials_ro=[str(HOME / ".claude" / ".credentials.json")]))
    assert (str(HOME / ".claude" / ".credentials.json"),) * 2 in _binds(argv, "--ro-bind")


def _fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".claude" / "plugins").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    (home / ".agents").mkdir()
    return home


def _build_home(tmp_path: Path, home: Path, roots: list[str]) -> list[str]:
    m = _manifest(tmp_path, shared_ro=[], skill_roots_ro=roots)
    m["lanes"]["logos"]["llm_source"] = "claude_code"  # no trimmed codex store under a fake home
    return ws.build_bwrap_argv("logos", "p", home / ".hermes" / "profiles" / "p", tmp_path / "ws", m,
                               home=home, exists=os.path.exists)


def test_skill_roots_real_fs_accepts_all_three(tmp_path):
    home = _fake_home(tmp_path)
    roots = [str(home / ".claude" / "plugins"), str(home / ".claude" / "settings.json"), str(home / ".agents")]
    ro = _binds(_build_home(tmp_path, home, roots), "--ro-bind")
    for p in roots:
        assert (p, p) in ro


@pytest.mark.parametrize("rel", [".claude/plugins", ".claude/settings.json", ".agents"])
def test_skill_roots_symlink_pointing_outside_rejected(tmp_path, rel):
    home = _fake_home(tmp_path)
    target = home / rel
    if target.is_dir():
        target.rmdir()
    else:
        target.unlink()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "settings.json").write_text("{}\n", encoding="utf-8")
    target.symlink_to(outside / "settings.json" if rel.endswith(".json") else outside)
    with pytest.raises(ws.SandboxConfigError):
        _build_home(tmp_path, home, [str(target)])


def test_skill_roots_symlinked_parent_rejected(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    real = tmp_path / "elsewhere"
    (real / "plugins").mkdir(parents=True)
    (home / ".claude").symlink_to(real)
    with pytest.raises(ws.SandboxConfigError):
        _build_home(tmp_path, home, [str(home / ".claude" / "plugins")])


def test_skill_roots_wrong_kind_or_missing_rejected(tmp_path):
    home = _fake_home(tmp_path)
    (home / ".claude" / "settings.json").unlink()
    (home / ".claude" / "settings.json").mkdir()
    with pytest.raises(ws.SandboxConfigError):
        _build_home(tmp_path, home, [str(home / ".claude" / "settings.json")])
    (home / ".agents").rmdir()
    with pytest.raises(ws.SandboxConfigError):
        _build_home(tmp_path, home, [str(home / ".agents")])


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
    # the example manifest carries exactly the allow-listed skill roots, all ro
    assert set(m["skill_roots_ro"]) == set(ws.skill_roots_allowed(HOME))
    argv = ws.build_bwrap_argv("logos", "p", HOME / ".hermes" / "profiles" / "p", "/tmp/ws", m,
                               home=HOME, exists=lambda p: True)
    for p in m["skill_roots_ro"]:
        assert (p, p) in _binds(argv, "--ro-bind") and all(s != p for s, _ in _binds(argv, "--bind"))


# ---------------------------------------------------------------- wrap() decisions

CMD = ["hermes", "-p", "logos_covenant_lead", "chat", "-q", "x"]
ENFORCE = {"worker_sandbox": {"mode": "enforce", "lanes": ["logos"]}}


@pytest.fixture(autouse=True)
def _attachments_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(tmp_path / "att"))


def test_wrap_binds_only_this_cards_attachment_dir_rw(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "lane_for", lambda p: "logos")
    out = _wrap(tmp_path)
    own = tmp_path / "att" / "t_x"
    assert own.is_dir()
    rw = [s for s, _ in _binds(out, "--bind")]
    assert str(own) in rw
    assert str(tmp_path / "att") not in rw


def test_attachment_dir_under_forbidden_root_is_refused(tmp_path):
    with pytest.raises(ws.SandboxConfigError):
        _build(tmp_path, attachments_dir=str(HOME / ".ssh" / "t_x"))


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
    m = _manifest(tmp_path)
    m["lanes"]["logos"]["llm_source"] = "claude_code"  # codex path (real creds file) covered below
    out = _wrap(tmp_path, manifest=m)
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
    # claude_code lane: the live run must not require a creds file under the REAL home.
    m = _manifest(tmp_path, shared_ro=[], lanes={"logos": {"repos": [], "vault": [], "llm_source": "claude_code"}})
    argv = ws.build_bwrap_argv("logos", "p", tmp_path / "profile", tmp_path / "ws", m,
                               kanban_db_dir=tmp_path / "kanban", home=HOME)
    probe = (
        "test ! -e /home/chris/.hermes/secrets/Orion.env && test ! -e /home/chris/.hermes/secrets"
        " && test ! -e /home/chris/.ssh && test ! -e /home/chris/.hermes/cache/op_cache.json"
        " && test ! -e /home/chris/.hermes/auth.json"
        " && test ! -e /var/run/docker.sock && test ! -e /run/user/$(id -u)"
        " && test \"$HOME\" = \"%s\" && test \"$HERMES_ENV_SOURCED\" = 1 && test \"$HERMES_SANDBOX_NO_REFRESH\" = 1"
        " && test \"$PWD\" = \"%s\" && touch \"%s/ok\""
    ) % (tmp_path / "homes" / "logos", tmp_path / "ws", tmp_path / "ws")
    res = subprocess.run(argv + ["--", "/bin/sh", "-c", probe], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "ws" / "ok").exists()
