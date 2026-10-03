import jwt
import jwt.api_jwt as jwt_api
import pytest
from fastapi import HTTPException

from app.core.auth import (
    Principal,
    _decode_token_sync,
    _principal_from_claims,
    require_global_service_scope,
    require_node_scope,
    require_scope,
)
from app.core.config import settings
from app.models.placement_schemas import NodeHeartbeat
from app.routers.placement import heartbeat_node, run_placement
from tests.time_control import FIXED_NOW, FrozenDateTime


def test_principal_tenant_site_scope():
    p = Principal(
        subject="u1",
        roles=frozenset({"viewer"}),
        tenant_id="tenant-a",
        site_ids=frozenset({"site-1", "site-2"}),
    )
    assert p.can_access("tenant-a", "site-1")
    assert not p.can_access("tenant-a", "site-x")
    assert not p.can_access("tenant-b", "site-1")


def test_out_of_scope_is_hidden_as_not_found():
    p = Principal("u1", frozenset({"viewer"}), "tenant-a", frozenset({"site-1"}))
    with pytest.raises(HTTPException) as exc:
        require_scope(p, "tenant-b", "site-9")
    assert exc.value.status_code == 404


def test_hs256_test_token_decode_and_role_normalization(monkeypatch):
    monkeypatch.setattr(jwt_api, "datetime", FrozenDateTime)
    old = (
        settings.auth_jwks_url,
        settings.auth_hs256_secret,
        settings.auth_issuer,
        settings.auth_audience,
    )
    try:
        settings.auth_jwks_url = ""
        settings.auth_hs256_secret = "phase2b-ci-secret"
        settings.auth_issuer = "https://issuer.example/"
        settings.auth_audience = "intelligent-vms"
        token = jwt.encode(
            {
                "sub": "operator-1",
                "exp": int(FIXED_NOW.timestamp()) + 300,
                "iss": settings.auth_issuer,
                "aud": settings.auth_audience,
                "tenant_id": "tenant-a",
                "site_ids": ["site-1"],
                "roles": ["operator", "unknown-role"],
            },
            settings.auth_hs256_secret,
            algorithm="HS256",
        )
        claims = _decode_token_sync(token)
        principal = _principal_from_claims(claims)
        assert principal.subject == "operator-1"
        assert principal.roles == frozenset({"operator"})
        assert principal.can_access("tenant-a", "site-1")
    finally:
        (
            settings.auth_jwks_url,
            settings.auth_hs256_secret,
            settings.auth_issuer,
            settings.auth_audience,
        ) = old


def test_service_node_scope_must_match_node_id_claim():
    principal = Principal(
        subject="svc-node-a",
        roles=frozenset({"service"}),
        tenant_id="*",
        site_ids=frozenset({"*"}),
        node_id="node-a",
    )
    require_node_scope(principal, "node-a")
    with pytest.raises(HTTPException) as exc:
        require_node_scope(principal, "node-b")
    assert exc.value.status_code == 403


def test_service_node_scope_requires_node_id_claim():
    principal = Principal(
        subject="svc-no-claim",
        roles=frozenset({"service"}),
        tenant_id="*",
        site_ids=frozenset({"*"}),
    )
    with pytest.raises(HTTPException) as exc:
        require_node_scope(principal, "node-a")
    assert exc.value.status_code == 403


def test_admin_bypasses_node_scope_check():
    principal = Principal(
        subject="admin-1",
        roles=frozenset({"admin"}),
        tenant_id="*",
        site_ids=frozenset({"*"}),
    )
    require_node_scope(principal, "any-node")


def test_global_service_scope_rejects_node_scoped_service_principal():
    principal = Principal(
        subject="svc-node-a",
        roles=frozenset({"service"}),
        tenant_id="*",
        site_ids=frozenset({"*"}),
        node_id="node-a",
    )
    with pytest.raises(HTTPException) as exc:
        require_global_service_scope(principal)
    assert exc.value.status_code == 403


def test_principal_extracts_optional_node_id_claim():
    principal = _principal_from_claims(
        {
            "sub": "svc-node-a",
            "tenant_id": "*",
            "site_ids": ["*"],
            "roles": ["service"],
            "node_id": "node-a",
        }
    )
    assert principal.node_id == "node-a"


def test_node_routes_reject_service_token_for_other_node():
    principal = Principal(
        subject="svc-node-a",
        roles=frozenset({"service"}),
        tenant_id="*",
        site_ids=frozenset({"*"}),
        node_id="node-a",
    )

    class DummySession:
        async def get(self, *_args, **_kwargs):
            raise AssertionError("session should not be used when auth fails")

    with pytest.raises(HTTPException) as exc:
        asyncio_run(
            heartbeat_node(
                node_id="node-b",
                payload=NodeHeartbeat(
                    load={"ingress_mbps": 0.0},
                    observed_at=FIXED_NOW,
                ),
                session=DummySession(),
                principal=principal,
            )
        )
    assert exc.value.status_code == 403


def test_placement_run_allows_admin(monkeypatch):
    principal = Principal(
        subject="admin-1",
        roles=frozenset({"admin"}),
        tenant_id="*",
        site_ids=frozenset({"*"}),
    )

    async def fake_run():
        return {"scanned": 1, "moved": 1, "unplaced": 0, "cursor": "cam-1"}

    monkeypatch.setattr("app.routers.placement.run_placement_once", fake_run)
    result = asyncio_run(run_placement(principal=principal))
    assert result.scanned == 1
    assert result.moved == 1
    assert result.unplaced == 0


def asyncio_run(awaitable):
    import asyncio

    return asyncio.run(awaitable)


def test_node_heartbeat_cannot_override_admin_operational_state():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        NodeHeartbeat.model_validate(
            {"state": "active", "load": {"active_sources": 1}},
        )
