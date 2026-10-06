import asyncio
import base64
import hashlib
import hmac
import json
import time
import secrets

import pytest
import jwt
from fastapi import HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from app.core.auth import BROWSER_CSRF_COOKIE, BROWSER_SESSION_COOKIE, Principal, get_principal
from app.core.config import settings
from app.core.security_posture import validate_security_posture
from app.routers.auth_session import create_browser_session, delete_browser_session


def _token(secret: str) -> str:
    def enc(value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = enc({"alg": "HS256", "typ": "JWT"})
    payload = enc(
        {
            "sub": "field-admin",
            "exp": int(time.time()) + 600,
            "aud": "intelligent-vms",
            "roles": ["admin"],
            "tenant_id": "field-test",
            "site_ids": ["site-01"],
        }
    )
    signature = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.{signature}"


def _request(method: str, *, cookie: str = "", csrf: str = "") -> Request:
    headers = []
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    if csrf:
        headers.append((b"x-vms-csrf", csrf.encode()))
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/api/v1/cameras",
            "headers": headers,
            "query_string": b"",
            "scheme": "http",
            "server": ("localhost", 8080),
            "client": ("127.0.0.1", 12345),
        }
    )


def test_browser_cookie_auth_reuses_existing_jwt_and_requires_csrf(monkeypatch):
    secret = "field-test-secret-that-is-long-enough"
    token = _token(secret)
    monkeypatch.setattr(settings, "auth_disabled", False)
    monkeypatch.setattr(settings, "auth_browser_session_enabled", True)
    monkeypatch.setattr(settings, "auth_hs256_secret", secret)
    monkeypatch.setattr(settings, "auth_issuer", "")
    monkeypatch.setattr(settings, "auth_audience", "intelligent-vms")

    principal = asyncio.run(
        get_principal(_request("GET", cookie=f"{BROWSER_SESSION_COOKIE}={token}"), None)
    )
    assert principal.tenant_id == "field-test"
    assert principal.site_ids == frozenset({"site-01"})

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            get_principal(
                _request("POST", cookie=f"{BROWSER_SESSION_COOKIE}={token}"),
                None,
            )
        )
    assert exc.value.status_code == 403

    cookie = f"{BROWSER_SESSION_COOKIE}={token}; {BROWSER_CSRF_COOKIE}=csrf-value"
    principal = asyncio.run(
        get_principal(_request("POST", cookie=cookie, csrf="csrf-value"), None)
    )
    assert principal.has_any_role("admin")


def test_browser_session_exchange_is_httponly_strict_and_does_not_return_token(monkeypatch):
    monkeypatch.setattr(settings, "auth_browser_session_enabled", True)
    monkeypatch.setattr(settings, "auth_browser_session_cookie_secure", False)
    monkeypatch.setattr(settings, "auth_browser_session_max_age_seconds", 3600)
    raw = "opaque-validated-token"
    principal = Principal(
        "field-admin", frozenset({"admin"}), "field-test", frozenset({"site-01"})
    )
    response = Response()
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw)

    payload = asyncio.run(create_browser_session(response, credentials, principal))
    cookies = "\n".join(response.headers.getlist("set-cookie"))
    assert payload["authenticated"] is True
    assert raw not in repr(payload)
    assert f"{BROWSER_SESSION_COOKIE}={raw}" in cookies
    assert "HttpOnly" in cookies
    assert "SameSite=strict" in cookies
    assert BROWSER_CSRF_COOKIE in cookies
    csrf_cookie = next(line for line in response.headers.getlist("set-cookie") if line.startswith(BROWSER_CSRF_COOKIE + "="))
    assert "Path=/" in csrf_cookie
    assert "HttpOnly" not in csrf_cookie


def test_production_oidc_rejects_insecure_browser_cookie(monkeypatch):
    monkeypatch.setattr(settings, "auth_require_oidc", True)
    monkeypatch.setattr(settings, "auth_disabled", False)
    monkeypatch.setattr(settings, "auth_browser_session_enabled", True)
    monkeypatch.setattr(settings, "auth_browser_session_cookie_secure", False)
    monkeypatch.setattr(settings, "auth_hs256_secret", "")
    with pytest.raises(RuntimeError, match="secure browser-session cookies"):
        validate_security_posture()


@pytest.mark.parametrize("expired", [False, True])
def test_browser_exchange_rejects_invalid_signature_or_expired_token(monkeypatch, expired):
    secret = secrets.token_urlsafe(32)
    monkeypatch.setattr(settings, "auth_disabled", False)
    monkeypatch.setattr(settings, "auth_browser_session_enabled", True)
    monkeypatch.setattr(settings, "auth_hs256_secret", secret)
    monkeypatch.setattr(settings, "auth_jwks_url", "")
    monkeypatch.setattr(settings, "auth_issuer", "")
    monkeypatch.setattr(settings, "auth_audience", "intelligent-vms")
    token = jwt.encode(
        {"sub": "temporary", "exp": 0 if expired else 4102444800,
         "aud": "intelligent-vms", "roles": ["admin"], "tenant_id": "field-test", "site_ids": ["site-01"]},
        secret if expired else secrets.token_urlsafe(32), algorithm="HS256",
    )
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_principal(_request("POST"), credentials))
    assert exc.value.status_code == 401


def test_cookie_mutation_rejects_mismatched_csrf(monkeypatch):
    monkeypatch.setattr(settings, "auth_disabled", False)
    monkeypatch.setattr(settings, "auth_browser_session_enabled", True)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_principal(_request("DELETE", cookie="vms_session=temporary; vms_csrf=expected", csrf="wrong"), None))
    assert exc.value.status_code == 403


def test_session_logout_clears_both_cookie_paths(monkeypatch):
    monkeypatch.setattr(settings, "auth_browser_session_cookie_secure", True)
    response = Response()
    principal = Principal("temporary", frozenset({"admin"}), "field-test", frozenset({"site-01"}))
    payload = asyncio.run(delete_browser_session(response, principal))
    assert payload == {"authenticated": False}
    cookies = response.headers.getlist("set-cookie")
    assert len(cookies) == 2
    assert all("Max-Age=0" in cookie and "SameSite=strict" in cookie and "Secure" in cookie for cookie in cookies)
    assert any(cookie.startswith("vms_session=") and "Path=/api" in cookie for cookie in cookies)
    assert any(cookie.startswith("vms_csrf=") and "Path=/;" in cookie for cookie in cookies)
