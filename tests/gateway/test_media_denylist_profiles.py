"""Regression test for the NorCal port of upstream #107609 (2026-10-04).

The media-delivery denylist must cover the credential/session stores of EVERY profile under
``<root>/profiles/*``, not only the launch home — otherwise ``MEDIA:<root>/profiles/<other>/.env``
(or auth.json, config.yaml, state.db) validates as deliverable and is uploaded to the chat.
Uses a sandboxed fake Hermes root; never touches real files.
"""
from pathlib import Path

import pytest

import gateway.platforms.base as base


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    for prof in ("alpha", "beta"):
        d = root / "profiles" / prof
        (d / "cache" / "images").mkdir(parents=True)
        (d / "sessions").mkdir()
        for name in (".env", "auth.json", "config.yaml", "state.db", "state.db-wal", "kanban.db"):
            (d / name).write_text("secret")
        (d / "sessions" / "s1.json").write_text("{}")
        (d / "cache" / "images" / "gen.png").write_bytes(b"\x89PNG")
    (root / ".env").write_text("secret")
    (root / "state.db").write_text("secret")
    monkeypatch.setattr(base, "_HERMES_ROOT", root)
    monkeypatch.setattr(base, "_HERMES_HOME", root)
    monkeypatch.setattr(base, "get_hermes_home", lambda: root)
    return root


@pytest.mark.parametrize("rel", [
    "profiles/beta/.env", "profiles/beta/auth.json", "profiles/beta/config.yaml",
    "profiles/beta/state.db", "profiles/beta/state.db-wal", "profiles/beta/kanban.db",
    "profiles/beta/sessions/s1.json", "profiles/alpha/.env", ".env", "state.db",
])
def test_other_profile_credentials_are_denied(fake_root, rel):
    target = (fake_root / rel).resolve()
    denied = [p.resolve() for p in base._media_delivery_denied_paths()]
    assert any(target == d or d in target.parents for d in denied), f"{rel} not covered by denylist"


def test_credential_home_roots_enumerates_every_profile(fake_root):
    roots = {p.resolve() for p in base._credential_home_roots()}
    assert (fake_root / "profiles" / "alpha").resolve() in roots
    assert (fake_root / "profiles" / "beta").resolve() in roots
    assert fake_root.resolve() in roots


def test_profile_cache_media_not_denied(fake_root):
    """Generated media in a profile's cache must stay deliverable (not swept up by the denylist)."""
    target = (fake_root / "profiles" / "beta" / "cache" / "images" / "gen.png").resolve()
    denied = [p.resolve() for p in base._media_delivery_denied_paths()]
    assert not any(target == d or d in target.parents for d in denied)
