"""Append-only company records: read-only lane vault + submissions dir."""
from pathlib import Path

import pytest

from hermes_cli import worker_sandbox as ws

HOME = Path("/home/chris")
VAULT_DIR = "/home/chris/services/life-wiki/vault/Business/Companies/Logos-Covenant-Inc"
VAULT_MD = "/home/chris/services/life-wiki/vault/Business/Companies/Logos-Covenant-Inc.md"
SUB = "/home/chris/.hermes/company-submissions/logos"


def _m(tmp_path, **top):
    m = {
        "version": 1,
        "sandbox_home_root": str(tmp_path / "homes"),
        "shared_ro": [],
        "credentials_ro": [],
        "groups": {},
        "lanes": {
            "logos": {"repos": [], "vault": [VAULT_DIR, VAULT_MD], "llm_source": "claude_code"},
            "ncass": {"repos": [], "vault": ["/home/chris/services/life-wiki/vault/Business/Companies/NCASS"]},
        },
    }
    m.update(top)
    return m


def _argv(tmp_path, m, exists=lambda p: True):
    return ws.build_bwrap_argv("logos", "logos_covenant_lead",
                               HOME / ".hermes" / "profiles" / "logos_covenant_lead",
                               tmp_path / "ws", m, home=HOME, exists=exists)


def _pairs(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def test_default_vault_stays_rw_and_no_submissions(tmp_path):
    argv = _argv(tmp_path, _m(tmp_path))
    assert VAULT_DIR in _pairs(argv, "--bind") and VAULT_DIR not in _pairs(argv, "--ro-bind")
    assert SUB not in _pairs(argv, "--bind")


def test_vault_readonly_binds_vault_ro_and_submissions_rw(tmp_path):
    argv = _argv(tmp_path, _m(tmp_path, vault_readonly=True))
    ro, rw = _pairs(argv, "--ro-bind"), _pairs(argv, "--bind")
    assert VAULT_DIR in ro and VAULT_MD in ro
    assert VAULT_DIR not in rw and VAULT_MD not in rw
    assert SUB in rw


def test_missing_submissions_dir_refuses(tmp_path):
    with pytest.raises(ws.SandboxConfigError, match="submissions dir"):
        _argv(tmp_path, _m(tmp_path, vault_readonly=True), exists=lambda p: p != SUB)


@pytest.mark.parametrize("bad", ["yes", 1, 0, [], {}])
def test_vault_readonly_must_be_bool(tmp_path, bad):
    with pytest.raises(ws.SandboxConfigError, match="vault_readonly"):
        _argv(tmp_path, _m(tmp_path, vault_readonly=bad))


def test_probe_rows_for_readonly_vault(tmp_path):
    checks = ws.probe_checks("logos", "logos_covenant_lead",
                             HOME / ".hermes" / "profiles" / "logos_covenant_lead", tmp_path / "ws",
                             _m(tmp_path, vault_readonly=True), home=HOME,
                             exists=lambda p: True, listdir=lambda d: [], network=False)
    by = {c["name"]: c for c in checks}
    assert by["own_vault_Logos-Covenant-Inc_readonly"]["kind"] == "readonly"
    assert by["own_vault_Logos-Covenant-Inc_md_readonly"]["file"] is True
    assert by["submissions_dir_rw"]["target"] == SUB
    hidden = [p for c in checks if c["name"] == "other_lane_repos_and_vault_hidden" for p in c["target"]]
    assert "/home/chris/services/life-wiki/vault/Business/Companies/NCASS" in hidden
    assert VAULT_DIR not in hidden
