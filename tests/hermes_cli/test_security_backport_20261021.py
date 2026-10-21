"""Regression pins for the 2026-10-21 hand-ported upstream v0.21.6 security fixes."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from hermes_cli.dashboard_auth import audit, middleware, routes, token_auth
from plugins.platforms.email.adapter import _extract_email_address


@pytest.mark.parametrize("mod", [routes, middleware, token_auth])
def test_client_ip_ignores_spoofed_x_forwarded_for(mod):
    req = SimpleNamespace(headers={"x-forwarded-for": "1.2.3.4"}, client=SimpleNamespace(host="10.0.0.9"))
    assert mod._client_ip(req) == "10.0.0.9"


def test_email_display_name_cannot_spoof_allowlisted_address():
    raw = '"Victim <victim@example.com>" <attacker@evil.test>'
    assert _extract_email_address(raw) == "attacker@evil.test"
    assert _extract_email_address("Chris <Chris@Example.com>") == "chris@example.com"


@pytest.mark.parametrize("uri", [
    "http://127.0.0.1@evil.test/cb",
    "http://evil.test\\@127.0.0.1/cb",
    "http://127.0.0.1:8080/cb#x",
    "http://localhost/cb",
])
def test_native_redirect_rejects_noncanonical_loopback(uri):
    with pytest.raises(HTTPException):
        routes._validate_loopback_redirect_uri(uri)


def test_native_redirect_accepts_canonical_loopback():
    assert routes._validate_loopback_redirect_uri("http://127.0.0.1:5000/cb") == "http://127.0.0.1:5000/cb"


def test_password_provider_is_length_bounded():
    with pytest.raises(Exception):
        routes._PasswordLoginBody(provider="x" * 129, username="u", password="p")


def test_audit_values_bounded_and_log_rotates(tmp_path, monkeypatch):
    log = tmp_path / "dashboard-auth.log"
    monkeypatch.setattr(audit, "_resolve_log_path", lambda: log)
    monkeypatch.setattr(audit, "_MAX_BYTES", 400)
    audit.audit_log(audit.AuditEvent.LOGIN_FAILURE, provider="p" * 10000)
    assert len(log.read_text()) < 400
    for _ in range(5):
        audit.audit_log(audit.AuditEvent.LOGIN_FAILURE, provider="p" * 300)
    assert (tmp_path / "dashboard-auth.log.1").exists()
