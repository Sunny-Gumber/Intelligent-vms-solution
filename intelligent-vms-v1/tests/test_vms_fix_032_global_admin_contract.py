"""Global-admin contract: only role admin with tenant_id '*' mutates global state.

Synthetic identities only. Tenant-scoped admins must receive HTTP 403 and leave
rows unchanged. The route inventory fails if a future global-mutation route is
mounted without require_global_admin.
"""

import ast
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.auth import Principal, get_principal, require_global_service_scope
from app.db.base import Base
from app.db.session import get_session
from app.main import app as control_app
from app.models.entities import CameraEntity, EventOutboxEntity
from app.models.placement import (
    InfrastructureNodeEntity,
    PlacementAssignmentEntity,
    PlacementRevocationEntity,
    SiteRegionEntity,
)
from app.routers import cameras, placement, system
from app.services import outbox as outbox_service
from app.services import placement as placement_service


DENIED = "Global administrator scope required"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
GLOBAL_PREFIXES = ("/api/v1/infrastructure/", "/api/v1/system/outbox/")
# Site-to-region mapping is tenant/site scoped today and also a placement input.
# C2 does not say which of those it is, so this route stays on its old dependency.
ESCALATED = frozenset({("PUT", "/api/v1/infrastructure/sites/region")})
NODE_SERVICE = frozenset(
    {
        ("POST", "/api/v1/infrastructure/nodes/{node_id}/heartbeat"),
        (
            "POST",
            "/api/v1/infrastructure/nodes/{node_id}/fences/revocations/{revocation_id}/ack",
        ),
    }
)
# Mutating control-plane routes that are not global-state mutations.
# A new mutating route that is absent here and lacks require_global_admin fails.
KNOWN_NON_GLOBAL = frozenset(
    {
        ("DELETE", "/api/v1/alarms/rules/{rule_id}"),
        ("DELETE", "/api/v1/auth/session"),
        ("DELETE", "/api/v1/camera-groups/{group_id}"),
        ("DELETE", "/api/v1/cameras/{camera_id}"),
        ("DELETE", "/api/v1/onvif/cameras/{camera_id}/osds/{osd_token}"),
        ("DELETE", "/api/v1/onvif/cameras/{camera_id}/privacy-masks/{role}/{mask_token}"),
        ("DELETE", "/api/v1/onvif/cameras/{camera_id}/profiles/third"),
        ("PATCH", "/api/v1/alarms/rules/{rule_id}"),
        ("PATCH", "/api/v1/camera-groups/{group_id}"),
        ("PATCH", "/api/v1/cameras/{camera_id}"),
        ("PATCH", "/api/v1/cameras/{camera_id}/credentials"),
        ("PATCH", "/api/v1/onvif/cameras/{camera_id}/osds/{osd_token}"),
        ("PATCH", "/api/v1/onvif/cameras/{camera_id}/privacy-masks/{role}/{mask_token}"),
        ("POST", "/api/v1/ai/results"),
        ("POST", "/api/v1/alarms/{alarm_id}/acknowledge"),
        ("POST", "/api/v1/alarms/{alarm_id}/close"),
        ("POST", "/api/v1/alarms/rules"),
        ("POST", "/api/v1/auth/session"),
        ("POST", "/api/v1/camera-groups"),
        ("POST", "/api/v1/cameras"),
        ("POST", "/api/v1/cameras/{camera_id}/replace"),
        ("POST", "/api/v1/events"),
        ("POST", "/api/v1/live/cameras/{camera_id}/access"),
        ("POST", "/api/v1/manual-recordings/cameras/{camera_id}/start"),
        ("POST", "/api/v1/manual-recordings/{session_id}/stop"),
        ("POST", "/api/v1/onvif/cameras/{camera_id}/osds"),
        ("POST", "/api/v1/onvif/cameras/{camera_id}/privacy-masks/{role}"),
        ("POST", "/api/v1/onvif/cameras/{camera_id}/refresh"),
        ("POST", "/api/v1/onvif/discover"),
        ("POST", "/api/v1/onvif/onboard"),
        ("POST", "/api/v1/onvif/onboard/qr"),
        ("POST", "/api/v1/onvif/onboard/serial"),
        ("POST", "/api/v1/onvif/probe"),
        ("POST", "/api/v1/ptz/cameras/{camera_id}/move"),
        ("POST", "/api/v1/ptz/cameras/{camera_id}/stop"),
        ("POST", "/internal/v1/events/ingest"),
        ("POST", "/internal/v1/recording/segments/complete"),
        ("PUT", "/api/v1/ai/cameras/{camera_id}/policy"),
        ("PUT", "/api/v1/infrastructure/sites/region"),
        ("PUT", "/api/v1/onvif/cameras/{camera_id}/date-time"),
        ("PUT", "/api/v1/onvif/cameras/{camera_id}/encoder/{role}"),
        ("PUT", "/api/v1/onvif/cameras/{camera_id}/imaging/{role}"),
        ("PUT", "/api/v1/onvif/cameras/{camera_id}/ir"),
        ("PUT", "/api/v1/onvif/cameras/{camera_id}/orientation/{role}"),
        ("PUT", "/api/v1/onvif/cameras/{camera_id}/osds/camera-name"),
        ("PUT", "/api/v1/onvif/cameras/{camera_id}/profiles/{role}"),
        ("PUT", "/api/v1/onvif/cameras/{camera_id}/profiles/{role}/codec"),
        ("PUT", "/api/v1/onvif/cameras/{camera_id}/video-source-modes/{role}"),
        ("PUT", "/api/v1/onvif/cameras/{camera_id}/video-standard/{role}"),
        ("PUT", "/api/v1/recordings/cameras/{camera_id}/policy"),
        ("POST", "/api/v1/ai/models"),
    }
)
# Local spool accepts hook/node tokens and queues node-local items. It does not
# write control-plane tables. A new mutating route in these processes fails
# until it is classified.
KNOWN_OTHER_MUTATING = frozenset(
    {
        ("POST", "/v1/events", "services/regional-spool/main.py"),
        ("POST", "/v1/heartbeat", "services/regional-spool/main.py"),
        ("POST", "/v1/recording/segments/complete", "services/regional-spool/main.py"),
    }
)
OTHER_SERVICE_ROOTS = (
    "services/regional-spool/main.py",
    "services/node-agent/main.py",
    "services/placement-controller/main.py",
    "services/event-writer/main.py",
    "services/alarm-worker/main.py",
    "services/onvif-event-worker/main.py",
)
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
# Older than HEARTBEAT_BODY observed_at. An authorized heartbeat is then the
# newest observation, so these authorization tests still see the write.
HEARTBEAT_SEED_AT = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)
NODE_BODY = {
    "name": "renamed-by-tenant-admin",
    "region_id": "region-other",
    "roles": ["recording"],
    "state": "draining",
    "enabled": False,
    "endpoints": {"api_url": "http://198.51.100.10:9997"},
    "capacity": {"max_sources": 1},
}
HEARTBEAT_BODY = {
    "load": {"ingress_mbps": 9, "egress_mbps": 4, "active_sources": 4},
    "authority_mode": "fenced_degraded",
    "observed_at": "2026-10-08T00:00:00+00:00",
}


def _admin(tenant_id: str) -> Principal:
    return Principal("synthetic-admin", frozenset({"admin"}), tenant_id, frozenset({"*"}))


def _service(node_id: str) -> Principal:
    return Principal(
        "synthetic-node-service",
        frozenset({"service"}),
        "*",
        frozenset({"*"}),
        node_id=node_id,
    )


def _freeze(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _row_state(row):
    if row is None:
        return None
    return tuple(
        (column.name, _freeze(getattr(row, column.name))) for column in row.__table__.columns
    )


def _iter_api_routes(routes):
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _iter_api_routes(original.routes)


def _dependency_mark(route: APIRoute):
    marked = False
    allow_node_service = None
    stack = [route.dependant]
    while stack:
        dependant = stack.pop()
        call = dependant.call
        if getattr(call, "__vms_global_admin_dependency__", False):
            marked = True
            allow_node_service = bool(getattr(call, "__vms_allow_node_service__", False))
        stack.extend(dependant.dependencies)
    return marked, allow_node_service


def _mutating_control_routes():
    found = []
    for route in _iter_api_routes(control_app.routes):
        methods = MUTATING & set(route.methods or [])
        marked, allow_node_service = _dependency_mark(route)
        for method in sorted(methods):
            found.append((method, route.path, route.endpoint.__name__, marked, allow_node_service))
    return found


def _decorator_routes(path: str):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=path)
    found = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            method = decorator.func.attr.upper()
            if method not in MUTATING or not decorator.args:
                continue
            literal = decorator.args[0]
            if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                found.append((method, literal.value))
    return found


def _other_service_routes():
    root = Path(__file__).parents[1]
    found = []
    for relative in OTHER_SERVICE_ROOTS:
        for method, route_path in _decorator_routes(str(root / relative)):
            found.append((method, route_path, relative))
    return found


@asynccontextmanager
async def _api(*routers):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    for router in routers:
        app.include_router(router)
    holder = {"principal": _admin("tenant-a")}

    async def identity():
        return holder["principal"]

    async def session_dependency():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_principal] = identity
    app.dependency_overrides[get_session] = session_dependency
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, sessions, holder
    finally:
        await engine.dispose()


def _field(state, name):
    return dict(state)[name]


async def _seed_nodes(sessions, heartbeat_at=NOW):
    async with sessions() as session:
        session.add(
            InfrastructureNodeEntity(
                id="node-a",
                name="node-a",
                region_id="default-region",
                roles_json=["media"],
                state="active",
                enabled=True,
                endpoints_json={"api_url": "http://198.51.100.20:9997"},
                capacity_json={
                    "max_ingress_mbps": 1000,
                    "max_egress_mbps": 1000,
                    "max_sources": 100,
                },
                load_json={"ingress_mbps": 1, "egress_mbps": 1, "active_sources": 1},
                heartbeat_at=heartbeat_at,
                authority_mode="central_online",
                generation=3,
            )
        )
        session.add(
            InfrastructureNodeEntity(
                id="node-b",
                name="node-b",
                region_id="default-region",
                roles_json=["media"],
                state="active",
                enabled=True,
                endpoints_json={},
                capacity_json={
                    "max_ingress_mbps": 1000,
                    "max_egress_mbps": 1000,
                    "max_sources": 100,
                },
                load_json={"ingress_mbps": 1, "egress_mbps": 1, "active_sources": 1},
                heartbeat_at=heartbeat_at,
                authority_mode="central_online",
            )
        )
        await session.commit()


async def _one(sessions, model, identity):
    async with sessions() as session:
        return _row_state(await session.get(model, identity))


async def _count(sessions, model):
    async with sessions() as session:
        return len((await session.execute(select(model))).scalars().all())


def _assert_denied(response: httpx.Response) -> None:
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == DENIED


def test_tenant_admin_cannot_register_configure_or_drain_nodes():
    async def scenario():
        async with _api(placement.router) as (client, sessions, holder):
            await _seed_nodes(sessions)
            before = await _one(sessions, InfrastructureNodeEntity, "node-a")
            before_count = await _count(sessions, InfrastructureNodeEntity)
            holder["principal"] = _admin("tenant-a")
            drained = await client.put("/api/v1/infrastructure/nodes/node-a", json=NODE_BODY)
            created = await client.put("/api/v1/infrastructure/nodes/node-new", json=NODE_BODY)
            _assert_denied(drained)
            _assert_denied(created)
            assert await _one(sessions, InfrastructureNodeEntity, "node-a") == before
            assert await _one(sessions, InfrastructureNodeEntity, "node-new") is None
            assert await _count(sessions, InfrastructureNodeEntity) == before_count

            holder["principal"] = _service("node-a")
            registered = await client.put("/api/v1/infrastructure/nodes/node-a", json=NODE_BODY)
            _assert_denied(registered)
            assert await _one(sessions, InfrastructureNodeEntity, "node-a") == before

            holder["principal"] = _admin("*")
            allowed = await client.put("/api/v1/infrastructure/nodes/node-a", json=NODE_BODY)
            assert allowed.status_code == 200, allowed.text
            assert allowed.json()["state"] == "draining"
            assert allowed.json()["name"] == "renamed-by-tenant-admin"
            stored = await _one(sessions, InfrastructureNodeEntity, "node-a")
            assert stored != before
            assert ("state", "draining") in stored
            created_ok = await client.put(
                "/api/v1/infrastructure/nodes/node-new",
                json={**NODE_BODY, "name": "node-new", "state": "active"},
            )
            assert created_ok.status_code == 200, created_ok.text
            assert await _one(sessions, InfrastructureNodeEntity, "node-new") is not None

    asyncio.run(scenario())


def test_tenant_admin_cannot_heartbeat_and_global_admin_can():
    async def scenario():
        async with _api(placement.router) as (client, sessions, holder):
            await _seed_nodes(sessions, heartbeat_at=HEARTBEAT_SEED_AT)
            before = await _one(sessions, InfrastructureNodeEntity, "node-a")
            holder["principal"] = _admin("tenant-a")
            denied = await client.post(
                "/api/v1/infrastructure/nodes/node-a/heartbeat", json=HEARTBEAT_BODY
            )
            _assert_denied(denied)
            assert await _one(sessions, InfrastructureNodeEntity, "node-a") == before

            holder["principal"] = _admin("*")
            allowed = await client.post(
                "/api/v1/infrastructure/nodes/node-a/heartbeat", json=HEARTBEAT_BODY
            )
            assert allowed.status_code == 200, allowed.text
            body = allowed.json()
            assert body["authority_mode"] == "fenced_degraded"
            assert body["load"]["active_sources"] == 4
            assert body["state"] == "active"
            assert await _one(sessions, InfrastructureNodeEntity, "node-a") != before

    asyncio.run(scenario())


def test_node_service_can_heartbeat_only_its_node():
    async def scenario():
        async with _api(placement.router) as (client, sessions, holder):
            await _seed_nodes(sessions, heartbeat_at=HEARTBEAT_SEED_AT)
            before = await _one(sessions, InfrastructureNodeEntity, "node-a")
            holder["principal"] = _service("node-b")
            denied = await client.post(
                "/api/v1/infrastructure/nodes/node-a/heartbeat", json=HEARTBEAT_BODY
            )
            assert denied.status_code == 403, denied.text
            assert await _one(sessions, InfrastructureNodeEntity, "node-a") == before

            holder["principal"] = _service("node-a")
            allowed = await client.post(
                "/api/v1/infrastructure/nodes/node-a/heartbeat", json=HEARTBEAT_BODY
            )
            assert allowed.status_code == 200, allowed.text
            assert allowed.json()["load"]["active_sources"] == 4

    asyncio.run(scenario())


async def _seed_revocation(sessions, revocation_id="rev-1"):
    await _seed_nodes(sessions)
    async with sessions() as session:
        session.add(
            CameraEntity(
                id="cam-a",
                tenant_id="tenant-a",
                site_id="site-a",
                name="Lobby",
                host="192.0.2.10",
                main_path="/main",
                stream_key="cam-a",
                desired_state="assigned",
            )
        )
        session.add(
            PlacementAssignmentEntity(
                id="asg-1",
                camera_id="cam-a",
                role="media",
                region_id="default-region",
                node_id="node-b",
                generation=2,
                active=True,
                lease_expires_at=NOW,
                cleanup_node_ids_json=["node-a"],
            )
        )
        session.add(
            PlacementRevocationEntity(
                id=revocation_id,
                assignment_id="asg-1",
                camera_id="cam-a",
                role="media",
                node_id="node-a",
                revoked_generation=1,
                execution_keys_json=["cam-a"],
                acknowledged_at=None,
            )
        )
        await session.commit()


def test_tenant_admin_cannot_ack_revocation_and_global_admin_can():
    async def scenario():
        async with _api(placement.router) as (client, sessions, holder):
            await _seed_revocation(sessions)
            before = await _one(sessions, PlacementRevocationEntity, "rev-1")
            assignment_before = await _one(sessions, PlacementAssignmentEntity, "asg-1")
            holder["principal"] = _admin("tenant-a")
            denied = await client.post(
                "/api/v1/infrastructure/nodes/node-a/fences/revocations/rev-1/ack"
            )
            _assert_denied(denied)
            assert await _one(sessions, PlacementRevocationEntity, "rev-1") == before
            assert await _one(sessions, PlacementAssignmentEntity, "asg-1") == assignment_before

            holder["principal"] = _admin("*")
            allowed = await client.post(
                "/api/v1/infrastructure/nodes/node-a/fences/revocations/rev-1/ack"
            )
            assert allowed.status_code == 200, allowed.text
            assert allowed.json()["acknowledged"] is True
            after = await _one(sessions, PlacementRevocationEntity, "rev-1")
            assert after != before
            assert any(name == "acknowledged_at" and value is not None for name, value in after)

    asyncio.run(scenario())


def test_node_service_can_ack_its_own_revocation():
    async def scenario():
        async with _api(placement.router) as (client, sessions, holder):
            await _seed_revocation(sessions)
            holder["principal"] = _service("node-b")
            denied = await client.post(
                "/api/v1/infrastructure/nodes/node-a/fences/revocations/rev-1/ack"
            )
            assert denied.status_code == 403, denied.text
            assert _field(await _one(sessions, PlacementRevocationEntity, "rev-1"), "acknowledged_at") is None

            holder["principal"] = _service("node-a")
            allowed = await client.post(
                "/api/v1/infrastructure/nodes/node-a/fences/revocations/rev-1/ack"
            )
            assert allowed.status_code == 200, allowed.text
            after = await _one(sessions, PlacementRevocationEntity, "rev-1")
            assert any(name == "acknowledged_at" and value is not None for name, value in after)

    asyncio.run(scenario())


def test_tenant_admin_cannot_run_placement_and_global_admin_can(monkeypatch):
    async def scenario():
        async with _api(placement.router) as (client, sessions, holder):
            monkeypatch.setattr(placement_service, "SessionLocal", sessions)
            await _seed_nodes(sessions, heartbeat_at=datetime.now(timezone.utc))
            async with sessions() as session:
                session.add(
                    CameraEntity(
                        id="cam-place",
                        tenant_id="tenant-a",
                        site_id="site-a",
                        name="Placement",
                        host="192.0.2.11",
                        main_path="/main",
                        stream_key="cam-place",
                        enabled=True,
                        desired_state="pending-placement",
                    )
                )
                await session.commit()
            before = await _count(sessions, PlacementAssignmentEntity)
            holder["principal"] = _admin("tenant-a")
            denied = await client.post("/api/v1/infrastructure/placement/run")
            _assert_denied(denied)
            assert await _count(sessions, PlacementAssignmentEntity) == before

            holder["principal"] = _admin("*")
            allowed = await client.post("/api/v1/infrastructure/placement/run")
            assert allowed.status_code == 200, allowed.text
            body = allowed.json()
            assert body["scanned"] == 1
            assert await _count(sessions, PlacementAssignmentEntity) == before + 1

    asyncio.run(scenario())


def test_tenant_admin_cannot_requeue_outbox_and_global_admin_can(monkeypatch):
    async def scenario():
        async with _api(system.router) as (client, sessions, holder):
            monkeypatch.setattr(outbox_service, "SessionLocal", sessions)
            async with sessions() as session:
                session.add(
                    EventOutboxEntity(
                        id="msg-dead",
                        topic="vms.events",
                        key_text="tenant-a:cam-a",
                        payload_json={"event_id": "synthetic"},
                        status="dead",
                        attempts=4,
                        next_attempt_at=NOW,
                    )
                )
                await session.commit()
            before = await _one(sessions, EventOutboxEntity, "msg-dead")
            holder["principal"] = _admin("tenant-a")
            denied = await client.post("/api/v1/system/outbox/msg-dead/requeue")
            _assert_denied(denied)
            assert await _one(sessions, EventOutboxEntity, "msg-dead") == before

            holder["principal"] = _admin("*")
            allowed = await client.post("/api/v1/system/outbox/msg-dead/requeue")
            assert allowed.status_code == 200, allowed.text
            assert allowed.json() == {"requeued": True, "id": "msg-dead"}
            after = await _one(sessions, EventOutboxEntity, "msg-dead")
            assert after != before
            assert ("status", "retry") in after
            assert ("attempts", 0) in after

    asyncio.run(scenario())


def test_tenant_admin_can_still_update_its_own_camera():
    async def scenario():
        async with _api(cameras.router) as (client, sessions, holder):
            async with sessions() as session:
                session.add(
                    CameraEntity(
                        id="cam-a",
                        tenant_id="tenant-a",
                        site_id="site-a",
                        name="Lobby",
                        host="192.0.2.10",
                        main_path="/main",
                        stream_key="cam-a",
                        desired_state="assigned",
                    )
                )
                session.add(
                    CameraEntity(
                        id="cam-b",
                        tenant_id="tenant-b",
                        site_id="site-b",
                        name="Other",
                        host="192.0.2.12",
                        main_path="/main",
                        stream_key="cam-b",
                        desired_state="assigned",
                    )
                )
                await session.commit()
            holder["principal"] = _admin("tenant-a")
            updated = await client.patch("/api/v1/cameras/cam-a", json={"name": "Lobby renamed"})
            assert updated.status_code == 200, updated.text
            assert updated.json()["name"] == "Lobby renamed"
            assert (await _one(sessions, CameraEntity, "cam-a")) and (
                "name",
                "Lobby renamed",
            ) in await _one(sessions, CameraEntity, "cam-a")
            other = await client.patch("/api/v1/cameras/cam-b", json={"name": "Stolen"})
            assert other.status_code == 404, other.text
            assert ("name", "Other") in await _one(sessions, CameraEntity, "cam-b")

    asyncio.run(scenario())


def test_escalated_site_region_route_keeps_tenant_admin_behavior():
    async def scenario():
        async with _api(placement.router) as (client, sessions, holder):
            holder["principal"] = _admin("tenant-a")
            created = await client.put(
                "/api/v1/infrastructure/sites/region",
                json={"tenant_id": "tenant-a", "site_id": "site-a", "region_id": "region-1"},
            )
            assert created.status_code == 200, created.text
            assert created.json()["region_id"] == "region-1"
            assert await _count(sessions, SiteRegionEntity) == 1
            holder["principal"] = _admin("tenant-b")
            hidden = await client.put(
                "/api/v1/infrastructure/sites/region",
                json={"tenant_id": "tenant-a", "site_id": "site-a", "region_id": "region-9"},
            )
            assert hidden.status_code == 404, hidden.text
            stored = (await _one(sessions, SiteRegionEntity, created.json()["id"]))
            assert ("region_id", "region-1") in stored

    asyncio.run(scenario())


def test_global_service_scope_rejects_tenant_scoped_admin():
    tenant_admin = _admin("tenant-a")
    with pytest.raises(HTTPException) as exc:
        require_global_service_scope(tenant_admin)
    assert exc.value.status_code == 403
    assert exc.value.detail == DENIED
    require_global_service_scope(_admin("*"))
    require_global_service_scope(_service_global())


def _service_global() -> Principal:
    return Principal("synthetic-global-service", frozenset({"service"}), "*", frozenset({"*"}))


def test_global_mutation_routes_depend_on_require_global_admin():
    routes = _mutating_control_routes()
    keys = {(method, path) for method, path, _name, _marked, _allow in routes}
    marked = {
        (method, path): allow
        for method, path, _name, is_marked, allow in routes
        if is_marked
    }
    expected_global = {
        (method, path)
        for method, path in keys
        if path.startswith(GLOBAL_PREFIXES) and (method, path) not in ESCALATED
    }
    assert marked.keys() == expected_global
    for key, allow in marked.items():
        assert allow is (key in NODE_SERVICE)
    non_global = keys - expected_global
    assert non_global == KNOWN_NON_GLOBAL
    assert ESCALATED <= non_global
    for method, path, _name, is_marked, _allow in routes:
        if (method, path) in ESCALATED:
            assert is_marked is False
    assert set(_other_service_routes()) == KNOWN_OTHER_MUTATING
    # The five global mutations, with the dependency on the route.
    assert expected_global == {
        ("PUT", "/api/v1/infrastructure/nodes/{node_id}"),
        ("POST", "/api/v1/infrastructure/nodes/{node_id}/heartbeat"),
        (
            "POST",
            "/api/v1/infrastructure/nodes/{node_id}/fences/revocations/{revocation_id}/ack",
        ),
        ("POST", "/api/v1/infrastructure/placement/run"),
        ("POST", "/api/v1/system/outbox/{message_id}/requeue"),
    }
