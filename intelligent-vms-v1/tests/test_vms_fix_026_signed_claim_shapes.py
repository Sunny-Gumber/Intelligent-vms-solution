"""Signed role and site claim shapes must be validated before a principal exists.

A JSON object decodes to a dict. Iterating that dict yields keys, so
{"admin": false} becomes the admin role and {"*": false} becomes every site.
Malformed shapes are rejected with HTTP 401. Unknown string roles still drop
to the existing HTTP 403. Missing site_ids still defaults to ["*"].
"""

import asyncio

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

import app.core.auth as auth
from app.core.config import settings


ISSUER = "https://issuer.example.test/"
AUDIENCE = "intelligent-vms"
HS256_SECRET = "vms-fix-026-hs256-secret-that-is-long-enough"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_KEY = PRIVATE_KEY.public_key()
ROLES_DETAIL = "Token roles claim has an invalid shape"
ROLE_DETAIL = "Token role claim has an invalid shape"
SITES_DETAIL = "Token site_ids claim has an invalid shape"


class _FakeSigningKey:
    """Expose the test RSA public key without fetching JWKS."""

    def __init__(self, key):
        self.key = key


class _FakeJWKClient:
    """Stand in for PyJWKClient so the RS256 path does not use the network."""

    def __init__(self, uri, cache_keys=True, lifespan=300):
        self.uri = uri

    def get_signing_key_from_jwt(self, token):
        return _FakeSigningKey(PUBLIC_KEY)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/cameras",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("localhost", 8080),
            "client": ("127.0.0.1", 12345),
        }
    )


def _claims(**overrides):
    claims = {
        "sub": "operator-1",
        "tenant_id": "tenant-a",
        "site_ids": ["site-1"],
        "roles": ["operator"],
    }
    claims.update(overrides)
    return claims


def _payload(**overrides):
    payload = {
        "sub": "operator-1",
        "exp": 4102444800,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "tenant_id": "tenant-a",
        "site_ids": ["site-1"],
        "roles": ["operator"],
    }
    payload.update(overrides)
    return payload


def _apply_verifier(monkeypatch, algorithm: str) -> None:
    monkeypatch.setattr(settings, "auth_disabled", False)
    monkeypatch.setattr(settings, "auth_issuer", ISSUER)
    monkeypatch.setattr(settings, "auth_audience", AUDIENCE)
    monkeypatch.setattr(auth, "_jwks_client", None)
    if algorithm == "HS256":
        monkeypatch.setattr(settings, "auth_jwks_url", "")
        monkeypatch.setattr(settings, "auth_hs256_secret", HS256_SECRET)
        return
    monkeypatch.setattr(settings, "auth_jwks_url", "https://issuer.example.test/jwks")
    monkeypatch.setattr(settings, "auth_hs256_secret", "")
    monkeypatch.setattr(auth, "PyJWKClient", _FakeJWKClient)


def _encode(algorithm: str, payload: dict) -> str:
    if algorithm == "HS256":
        return jwt.encode(payload, HS256_SECRET, algorithm="HS256")
    return jwt.encode(payload, PRIVATE_KEY, algorithm="RS256", headers={"kid": "vms-fix-026"})


def _assert_shape_rejected(claims: dict, detail: str) -> None:
    try:
        principal = auth._principal_from_claims(claims)
    except HTTPException as exc:
        assert exc.status_code == 401, exc.detail
        assert exc.detail == detail
        assert (exc.headers or {}).get("WWW-Authenticate") == "Bearer"
        return
    except Exception as exc:
        pytest.fail(
            f"expected HTTP 401 ({detail}), raised {type(exc).__name__}: {exc}"
        )
    pytest.fail(
        "expected HTTP 401 "
        f"({detail}), built principal roles={sorted(principal.roles)} "
        f"site_ids={sorted(principal.site_ids)}"
    )


def _assert_request_rejected(token: str, detail: str) -> None:
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    try:
        principal = asyncio.run(auth.get_principal(_request(), credentials))
    except HTTPException as exc:
        assert exc.status_code == 401, exc.detail
        assert exc.detail == detail
        assert (exc.headers or {}).get("WWW-Authenticate") == "Bearer"
        return
    except Exception as exc:
        pytest.fail(
            f"expected HTTP 401 ({detail}), raised {type(exc).__name__}: {exc}"
        )
    pytest.fail(
        "expected HTTP 401 "
        f"({detail}), built principal roles={sorted(principal.roles)} "
        f"site_ids={sorted(principal.site_ids)}"
    )


@pytest.mark.parametrize(
    "raw_roles",
    [
        pytest.param({"admin": False}, id="dict-admin-false"),
        pytest.param({"admin": True}, id="dict-admin-true"),
        pytest.param({"operator": False}, id="dict-operator-false"),
        pytest.param(None, id="null"),
        pytest.param(1, id="integer"),
        pytest.param(0, id="integer-zero"),
        pytest.param(1.5, id="float"),
        pytest.param(True, id="boolean-true"),
        pytest.param(False, id="boolean-false"),
        pytest.param([{"admin": False}], id="list-dict"),
        pytest.param([["admin"]], id="list-nested-list"),
        pytest.param([1], id="list-integer"),
        pytest.param([None], id="list-null"),
        pytest.param([True], id="list-boolean"),
        pytest.param(["admin", {"admin": False}], id="list-admin-plus-dict"),
        pytest.param(["operator", ["admin"]], id="list-operator-plus-nested"),
        pytest.param(["viewer", 1], id="list-viewer-plus-integer"),
    ],
)
def test_malformed_roles_never_become_a_role(raw_roles):
    _assert_shape_rejected(_claims(roles=raw_roles), ROLES_DETAIL)


def test_malformed_singular_role_never_becomes_admin():
    claims = _claims()
    del claims["roles"]
    claims["role"] = {"admin": False}
    _assert_shape_rejected(claims, ROLE_DETAIL)


@pytest.mark.parametrize(
    "raw_sites",
    [
        pytest.param({"*": False}, id="dict-star-false"),
        pytest.param({"*": True}, id="dict-star-true"),
        pytest.param({"site-1": False}, id="dict-site-false"),
        pytest.param(None, id="null"),
        pytest.param(1, id="integer"),
        pytest.param(True, id="boolean-true"),
        pytest.param(False, id="boolean-false"),
        pytest.param([{"*": False}], id="list-dict"),
        pytest.param([["*"]], id="list-nested-list"),
        pytest.param([1], id="list-integer"),
        pytest.param([None], id="list-null"),
        pytest.param(["site-1", {"*": False}], id="list-site-plus-dict"),
    ],
)
def test_malformed_site_ids_never_become_sites(raw_sites):
    _assert_shape_rejected(_claims(site_ids=raw_sites), SITES_DETAIL)


def test_string_and_list_roles_still_normalize():
    string_principal = auth._principal_from_claims(_claims(roles="operator"))
    list_principal = auth._principal_from_claims(
        _claims(roles=["operator", "unknown-role", "admin"])
    )
    assert string_principal.roles == frozenset({"operator"})
    assert list_principal.roles == frozenset({"operator", "admin"})
    assert list_principal.can_access("tenant-a", "site-1")
    assert not list_principal.can_access("tenant-a", "site-x")


def test_singular_string_role_still_works():
    claims = _claims()
    del claims["roles"]
    claims["role"] = "viewer"
    principal = auth._principal_from_claims(claims)
    assert principal.roles == frozenset({"viewer"})


def test_unknown_and_empty_string_roles_stay_403():
    for raw_roles in (["not-a-role"], [], [""], "not-a-role"):
        with pytest.raises(HTTPException) as exc:
            auth._principal_from_claims(_claims(roles=raw_roles))
        assert exc.value.status_code == 403
        assert exc.value.detail == "Token has no recognized VMS role"


def test_valid_site_shapes_and_missing_default_still_work():
    one = auth._principal_from_claims(_claims(site_ids="site-9"))
    many = auth._principal_from_claims(_claims(site_ids=["site-1", "site-2"]))
    wildcard = auth._principal_from_claims(_claims(site_ids="*"))
    claims = _claims()
    del claims["site_ids"]
    missing = auth._principal_from_claims(claims)
    assert one.site_ids == frozenset({"site-9"})
    assert many.site_ids == frozenset({"site-1", "site-2"})
    assert wildcard.site_ids == frozenset({"*"})
    assert missing.site_ids == frozenset({"*"})
    assert missing.can_access("tenant-a", "site-x")


@pytest.mark.parametrize("algorithm", ["HS256", "RS256"])
def test_signed_dict_roles_are_rejected(monkeypatch, algorithm):
    _apply_verifier(monkeypatch, algorithm)
    token = _encode(algorithm, _payload(roles={"admin": False}))
    _assert_request_rejected(token, ROLES_DETAIL)


@pytest.mark.parametrize("algorithm", ["HS256", "RS256"])
def test_signed_dict_site_ids_are_rejected(monkeypatch, algorithm):
    _apply_verifier(monkeypatch, algorithm)
    token = _encode(algorithm, _payload(site_ids={"*": False}))
    _assert_request_rejected(token, SITES_DETAIL)


@pytest.mark.parametrize("algorithm", ["HS256", "RS256"])
def test_signed_string_and_list_roles_still_normalize(monkeypatch, algorithm):
    _apply_verifier(monkeypatch, algorithm)
    string_token = _encode(algorithm, _payload(roles="viewer"))
    list_token = _encode(algorithm, _payload(roles=["operator", "unknown-role"]))
    string_credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=string_token)
    list_credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=list_token)
    string_principal = asyncio.run(auth.get_principal(_request(), string_credentials))
    list_principal = asyncio.run(auth.get_principal(_request(), list_credentials))
    assert string_principal.roles == frozenset({"viewer"})
    assert list_principal.roles == frozenset({"operator"})
    assert list_principal.site_ids == frozenset({"site-1"})
    assert list_principal.tenant_id == "tenant-a"


@pytest.mark.parametrize("algorithm", ["HS256", "RS256"])
def test_both_verifiers_use_the_same_principal_builder(monkeypatch, algorithm):
    _apply_verifier(monkeypatch, algorithm)
    seen = []
    original = auth._principal_from_claims

    def spy(claims):
        seen.append(claims)
        return original(claims)

    monkeypatch.setattr(auth, "_principal_from_claims", spy)
    token = _encode(algorithm, _payload(roles=["viewer"]))
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    principal = asyncio.run(auth.get_principal(_request(), credentials))
    assert principal.roles == frozenset({"viewer"})
    assert seen[0]["roles"] == ["viewer"]
