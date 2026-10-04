import asyncio

import pytest

from app.core.config import settings
from app.core.security_posture import validate_security_posture
from app.routers.auth_session import authentication_capabilities


def _set(monkeypatch, **overrides):
    values = {
        "auth_require_oidc": True,
        "auth_disabled": False,
        "auth_hs256_secret": "",
        "auth_jwks_url": "https://identity.example.test/.well-known/jwks.json",
        "auth_issuer": "https://identity.example.test/",
        "auth_audience": "intelligent-vms",
        "auth_desktop_oidc_enabled": True,
        "auth_oidc_client_id": "intelligent-vms-desktop",
        "auth_oidc_scopes": "openid profile offline_access",
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)


def test_auth_capabilities_expose_public_oidc_metadata_only(monkeypatch):
    _set(monkeypatch)
    payload = asyncio.run(authentication_capabilities())

    assert payload["authentication_required"] is True
    assert payload["manual_token_login"] is False
    assert payload["remember_session"] is True
    assert payload["oidc"] == {
        "enabled": True,
        "required": True,
        "authority": "https://identity.example.test/",
        "client_id": "intelligent-vms-desktop",
        "scopes": ["openid", "profile", "offline_access"],
        "callback": "loopback",
        "pkce_methods": ["S256"],
    }
    rendered = repr(payload).lower()
    assert "client_secret" not in rendered
    assert "jwks" not in rendered


def test_auth_capabilities_fail_safe_when_desktop_oidc_not_configured(monkeypatch):
    _set(monkeypatch, auth_desktop_oidc_enabled=False)
    payload = asyncio.run(authentication_capabilities())

    assert payload["oidc"]["enabled"] is False
    assert payload["oidc"]["required"] is True
    assert payload["oidc"]["authority"] == ""
    assert payload["oidc"]["client_id"] == ""
    assert payload["oidc"]["scopes"] == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("auth_oidc_client_id", "", "AUTH_OIDC_CLIENT_ID"),
        ("auth_oidc_scopes", "profile offline_access", "openid scope"),
        ("auth_oidc_scopes", "openid\nprofile", "invalid characters"),
    ],
)
def test_desktop_oidc_posture_rejects_incomplete_public_client_config(
    monkeypatch, field, value, message
):
    _set(monkeypatch, **{field: value})
    monkeypatch.setattr(
        "app.core.security_posture.validate_live_signing_key", lambda: None
    )

    with pytest.raises(RuntimeError, match=message):
        validate_security_posture()
