import asyncio
import base64
import hashlib
import logging
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.requests import Request
from starlette.responses import Response

from app.core.auth import Principal, get_principal
from app.core.config import settings
from app.core.security_posture import validate_security_posture
from app.security_audit import SecurityAuditMiddleware

TEST_LIVE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
TEST_LIVE_KEY_B64 = base64.b64encode(
    TEST_LIVE_KEY.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
).decode("ascii")


def _request(method: str, request_id: str | None = None) -> Request:
    headers = []
    if request_id is not None:
        headers.append((b"x-request-id", request_id.encode("ascii")))
    scope = {
        "type": "http",
        "method": method,
        "path": "/raw/camera/secret-value",
        "headers": headers,
        "query_string": b"token=must-not-log",
        "scheme": "https",
        "server": ("example.test", 443),
        "client": ("192.0.2.10", 12345),
    }
    request = Request(scope)
    request.scope["route"] = SimpleNamespace(path="/api/v1/cameras/{camera_id}")
    return request


def _set_oidc_settings(monkeypatch, **overrides):
    values = {
        "auth_require_oidc": True,
        "auth_disabled": False,
        "auth_hs256_secret": "",
        "auth_jwks_url": "https://identity.example.test/.well-known/jwks.json",
        "auth_issuer": "https://identity.example.test/",
        "auth_audience": "intelligent-vms",
        "live_view_token_private_key_b64": TEST_LIVE_KEY_B64,
        "live_view_token_verification_public_keys_b64_json": "[]",
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)


def test_production_oidc_posture_accepts_https_jwks(monkeypatch):
    """Accept a complete HTTPS OIDC trust configuration."""
    _set_oidc_settings(monkeypatch)

    validate_security_posture()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("auth_disabled", True, "AUTH_DISABLED"),
        ("auth_hs256_secret", "local-secret", "AUTH_HS256_SECRET"),
        ("auth_jwks_url", "", "AUTH_JWKS_URL"),
        ("auth_jwks_url", "http://identity.example.test/jwks", "HTTPS URL"),
        ("auth_issuer", "", "AUTH_ISSUER"),
        ("auth_issuer", "http://identity.example.test/", "HTTPS URL"),
        ("auth_audience", "", "AUTH_AUDIENCE"),
        ("live_view_token_private_key_b64", "", "live-view signing keyring"),
        ("live_view_token_private_key_b64", "not-base64", "live-view signing keyring"),
        ("live_view_token_verification_public_keys_b64_json", "not-json", "live-view signing keyring"),
    ],
)
def test_production_oidc_posture_fails_closed(monkeypatch, field, value, message):
    """Reject insecure or incomplete production OIDC settings."""
    _set_oidc_settings(monkeypatch, **{field: value})

    with pytest.raises(RuntimeError, match=message):
        validate_security_posture()


def test_oidc_posture_is_optional_for_local_profile(monkeypatch):
    """Preserve explicit local/test configurations when production mode is off."""
    _set_oidc_settings(
        monkeypatch,
        auth_require_oidc=False,
        auth_disabled=True,
        auth_hs256_secret="test-only",
        auth_jwks_url="",
        auth_issuer="",
    )

    validate_security_posture()


def test_get_principal_attaches_dev_identity_to_request(monkeypatch):
    """Attach resolved principals to request state for downstream audit logging."""
    monkeypatch.setattr(settings, "auth_disabled", True)
    request = _request("GET")

    principal = asyncio.run(get_principal(request, None))

    assert principal.subject == "development-bypass"
    assert request.state.principal is principal


def test_security_audit_logs_mutation_without_sensitive_request_data(caplog):
    """Audit a mutation using route templates and hashed actor identity only."""
    request = _request("POST", "request-12345678")
    request.state.principal = Principal(
        subject="user@example.test",
        roles=frozenset({"operator"}),
        tenant_id="tenant-01",
        site_ids=frozenset({"site-01"}),
    )
    middleware = SecurityAuditMiddleware(app=lambda *_args, **_kwargs: None)

    async def call_next(_request):
        return Response(status_code=204)

    with caplog.at_level(logging.INFO, logger="vms.security.audit"):
        response = asyncio.run(middleware.dispatch(request, call_next))

    expected_hash = hashlib.sha256(b"user@example.test").hexdigest()[:16]
    assert response.headers["x-request-id"] == "request-12345678"
    assert '"route":"/api/v1/cameras/{camera_id}"' in caplog.text
    assert f'"subject_hash":"{expected_hash}"' in caplog.text
    assert '"tenant_id":"tenant-01"' in caplog.text
    assert "user@example.test" not in caplog.text
    assert "must-not-log" not in caplog.text
    assert "/raw/camera/secret-value" not in caplog.text


def test_security_audit_logs_read_authorization_failure(caplog):
    """Audit GET authorization failures even though normal reads are not logged."""
    request = _request("GET")
    middleware = SecurityAuditMiddleware(app=lambda *_args, **_kwargs: None)

    async def call_next(_request):
        return Response(status_code=403)

    with caplog.at_level(logging.INFO, logger="vms.security.audit"):
        response = asyncio.run(middleware.dispatch(request, call_next))

    assert response.status_code == 403
    assert '"method":"GET"' in caplog.text
    assert '"status":403' in caplog.text
    assert '"subject_hash":"anonymous"' in caplog.text


def test_security_audit_escapes_untrusted_claim_control_characters(caplog):
    """Keep JWT-derived audit fields from injecting additional log lines."""
    request = _request("POST")
    request.state.principal = Principal(
        subject="user@example.test",
        roles=frozenset({"operator"}),
        tenant_id="tenant-01\\nforged=true",
        site_ids=frozenset({"site-01"}),
        node_id="node-01\\rforged=true",
    )
    middleware = SecurityAuditMiddleware(app=lambda *_args, **_kwargs: None)

    async def call_next(_request):
        return Response(status_code=204)

    with caplog.at_level(logging.INFO, logger="vms.security.audit"):
        asyncio.run(middleware.dispatch(request, call_next))

    assert "tenant-01\\\\nforged=true" in caplog.text
    assert "node-01\\\\rforged=true" in caplog.text
    assert "tenant-01\nforged=true" not in caplog.text
    assert "node-01\rforged=true" not in caplog.text
