"""Secret stores under HERMES_HOME must be write-denied, control files must not.

``get_read_block_error`` already refuses every credential store. The write side is
deliberately narrower: control files (``auth.json``, ``config.yaml``,
``webhook_subscriptions.json``) are freed on purpose -- true containment belongs in
Docker/remote backends and OS permissions, not an expanding hardcoded denylist (see
``tests/tools/test_write_deny.py::TestWriteAllowed::test_hermes_control_files_requested_writable``).

What stays write-denied is secret *material*: ``auth/google_oauth.json`` (an OAuth token
store), both Bitwarden caches, ``vault/`` (key + ciphertext side by side) and
``browser-profile/`` (copied cookies / Login Data). Ported from upstream e7cd1848c9 +
1c0d95badb (NousResearch/hermes-agent) onto this fork's ``agent/file_safety.py`` layout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import agent.file_safety as fs

# Secret material: write-denied.
SECRET_FILES = (
    ".env",
    ".anthropic_oauth.json",
    "auth/google_oauth.json",
    "cache/bws_cache.json",
    "cache/bws_cache.enc.json",
)

# Secret directories: write-denied, any file inside.
SECRET_DIRS = fs._WRITE_DENIED_SECRET_DIRS

# Control files that are read-denied but deliberately left writable.
WRITABLE_CONTROL_FILES = ("auth.json", "config.yaml", "webhook_subscriptions.json")


@pytest.fixture()
def hermes_layout(tmp_path, monkeypatch):
    """Profile HERMES_HOME plus a distinct global root, both patched."""
    root = tmp_path / "hermes_root"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True)
    monkeypatch.setattr(fs, "_hermes_home_path", lambda: profile)
    monkeypatch.setattr(fs, "_hermes_root_path", lambda: root)
    return root, profile


def _touch(base: Path, rel: str) -> Path:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("dummy", encoding="utf-8")
    return p


@pytest.mark.parametrize("name", SECRET_FILES)
def test_secret_files_are_write_denied(hermes_layout, name):
    root, profile = hermes_layout
    for base in (profile, root):
        path = _touch(base, name)
        assert fs.is_write_denied(str(path)), f"write allowed: {path}"


@pytest.mark.parametrize("sub", SECRET_DIRS)
def test_secret_dirs_are_write_denied(hermes_layout, sub):
    root, profile = hermes_layout
    for base in (profile, root):
        path = _touch(base, f"{sub}/inside.bin")
        assert fs.is_write_denied(str(path)), f"write allowed: {path}"


@pytest.mark.parametrize("name", WRITABLE_CONTROL_FILES)
def test_control_files_stay_writable(hermes_layout, name):
    _, profile = hermes_layout
    path = _touch(profile, name)
    assert fs.is_write_denied(str(path)) is False, f"{name} should be writable"


def test_nested_same_basename_file_stays_writable(hermes_layout):
    """A skill's own mock ``auth/google_oauth.json`` fixture, nested somewhere that is
    not the HERMES_HOME credential path, must not be swept up by the guard."""
    _, profile = hermes_layout
    path = _touch(profile, "skills/my-skill/auth/google_oauth.json")
    assert fs.is_write_denied(str(path)) is False


def test_secret_file_outside_hermes_home_stays_writable(hermes_layout, tmp_path):
    project = tmp_path / "myproject"
    path = _touch(project, "auth/google_oauth.json")
    assert fs.is_write_denied(str(path)) is False


def test_write_file_does_not_replace_vault_contents(hermes_layout):
    _, profile = hermes_layout
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations

    target = _touch(profile, "vault/vault.key")
    target.write_text("not-a-real-key\n", encoding="utf-8")
    ops = ShellFileOperations(LocalEnvironment(cwd=str(profile)), cwd=str(profile))
    res = ops.write_file(str(target), "stolen\n")
    assert res.error is not None
    assert target.read_text(encoding="utf-8") == "not-a-real-key\n"


def test_write_file_does_not_replace_browser_profile_contents(hermes_layout):
    _, profile = hermes_layout
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations

    target = _touch(profile, "browser-profile/Default/Login Data")
    target.write_text("sqlite-bytes\n", encoding="utf-8")
    ops = ShellFileOperations(LocalEnvironment(cwd=str(profile)), cwd=str(profile))
    res = ops.write_file(str(target), "stolen\n")
    assert res.error is not None
    assert target.read_text(encoding="utf-8") == "sqlite-bytes\n"
