"""Exercise alarm write scope through real role dependencies and persisted rows."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.auth import Principal, get_principal
from app.core.config import settings
from app.db.base import Base
from app.db.session import get_session
from app.models.entities import AlarmInstanceEntity, AlarmRuleEntity, CameraEntity
from app.routers.alarms import router
from app.services.alarm_rules import RuleSnapshot, rule_matches


def _principal(role="operator", sites=("site-a",), tenant="tenant-a"):
    return Principal("synthetic-actor", frozenset({role}), tenant, frozenset(sites))


def _payload(**changes):
    values = dict(tenant_id="tenant-a", name="Synthetic motion", event_types=["motion"])
    values.update(changes)
    return values


def _rule(rule_id="rule", cameras=(), site=None, tenant="tenant-a"):
    return AlarmRuleEntity(
        id=rule_id, tenant_id=tenant, site_id=site, name="Original", enabled=True,
        event_types_json=["motion"], camera_ids_json=list(cameras),
        severities_json=[], alarm_severity="high", cooldown_seconds=60,
    )


def _state(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


@asynccontextmanager
async def _api(principal, rules=()):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(router)

    async def identity():
        return principal

    async def session_dependency():
        async with sessions() as session:
            yield session

    # Substitute identity acquisition only; real require_roles and require_scope run.
    app.dependency_overrides[get_principal] = identity
    app.dependency_overrides[get_session] = session_dependency
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            for camera_id, tenant, site in (
                ("cam-a", "tenant-a", "site-a"),
                ("cam-a2", "tenant-a", "site-a"),
                ("cam-b", "tenant-a", "site-b"),
                ("cam-other", "tenant-b", "site-a"),
            ):
                session.add(CameraEntity(
                    id=camera_id, tenant_id=tenant, site_id=site, name=camera_id,
                    host="192.0.2.1", main_path="/synthetic", stream_key=camera_id,
                ))
            session.add_all(rules)
            await session.commit()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, sessions
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _enable_alarms(monkeypatch):
    monkeypatch.setattr(settings, "alarm_processing_enabled", True)


@pytest.mark.parametrize("role", ["operator", "admin"])
@pytest.mark.parametrize("filter_values", [{}, {"camera_ids": []}])
def test_site_limited_wildcard_create_adds_no_rows(role, filter_values):
    async def scenario():
        async with _api(_principal(role)) as (client, sessions):
            response = await client.post("/api/v1/alarms/rules", json=_payload(**filter_values))
            assert response.status_code == 404
            async with sessions() as session:
                assert not (await session.execute(select(AlarmRuleEntity))).scalars().all()
    asyncio.run(scenario())


@pytest.mark.parametrize("role,method,changes", [
    ("operator", "PATCH", {"name": "Changed"}),
    ("admin", "PATCH", {"enabled": False}),
    ("admin", "DELETE", None),
    ("operator", "PATCH", {"camera_ids": ["cam-a"]}),
])
@pytest.mark.parametrize("cameras", [[], ["cam-a", "cam-b"], ["missing"], ["cam-other"]])
def test_entire_existing_scope_required_and_denials_preserve_rows(role, method, changes, cameras):
    async def scenario():
        async with _api(_principal(role), [_rule(cameras=cameras)]) as (client, sessions):
            async with sessions() as session:
                before = _state(await session.get(AlarmRuleEntity, "rule"))
            response = await client.request(method, "/api/v1/alarms/rules/rule", json=changes)
            assert response.status_code == 404
            assert response.json() == {"detail": "Resource not found"}
            async with sessions() as session:
                assert _state(await session.get(AlarmRuleEntity, "rule")) == before
    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"site_id": "site-a"},
    {"camera_ids": ["cam-a", "cam-a2", "cam-a"]},
    {"site_id": "site-a", "camera_ids": ["cam-a"]},
])
def test_site_limited_create_and_metadata_patch_preserve_restrictions(changes):
    async def scenario():
        async with _api(_principal()) as (client, sessions):
            created = await client.post("/api/v1/alarms/rules", json=_payload(**changes))
            assert created.status_code == 201
            rule_id = created.json()["id"]
            updated = await client.patch(f"/api/v1/alarms/rules/{rule_id}", json={"name": "Changed"})
            assert updated.status_code == 200
            assert updated.json()["camera_ids"] == sorted(set(changes.get("camera_ids", [])))
            assert updated.json()["site_id"] == changes.get("site_id")
            async with sessions() as session:
                assert (await session.get(AlarmRuleEntity, rule_id)).name == "Changed"
    asyncio.run(scenario())


@pytest.mark.parametrize("camera_ids,site", [
    (["cam-a", "cam-b"], None), (["cam-a", "cam-other"], None),
    (["cam-a", "missing"], None), (["cam-b"], "site-a"),
])
def test_invalid_camera_sets_reject_create_and_patch_without_changes(camera_ids, site):
    async def scenario():
        async with _api(_principal(), [_rule(cameras=["cam-a"])]) as (client, sessions):
            async with sessions() as session:
                before = _state(await session.get(AlarmRuleEntity, "rule"))
            created = await client.post(
                "/api/v1/alarms/rules", json=_payload(site_id=site, camera_ids=camera_ids)
            )
            assert created.status_code == 404
            assert created.json() == {"detail": "Resource not found"}
            updated = await client.patch(
                "/api/v1/alarms/rules/rule", json={"name": "Changed", "camera_ids": camera_ids}
            )
            assert updated.status_code == 404
            assert updated.json() == {"detail": "Resource not found"}
            async with sessions() as session:
                assert _state(await session.get(AlarmRuleEntity, "rule")) == before
                assert len((await session.execute(select(AlarmRuleEntity))).scalars().all()) == 1
    asyncio.run(scenario())


def test_clearing_null_site_camera_filter_cannot_broaden_scope():
    async def scenario():
        async with _api(_principal(), [_rule(cameras=["cam-a"])]) as (client, sessions):
            async with sessions() as session:
                before = _state(await session.get(AlarmRuleEntity, "rule"))
            response = await client.patch(
                "/api/v1/alarms/rules/rule", json={"name": "Changed", "camera_ids": []}
            )
            assert response.status_code == 404
            async with sessions() as session:
                assert _state(await session.get(AlarmRuleEntity, "rule")) == before
    asyncio.run(scenario())


@pytest.mark.parametrize("field", [
    "name", "enabled", "event_types", "severities", "camera_ids", "alarm_severity", "cooldown_seconds",
])
def test_explicit_patch_null_is_validation_error_without_partial_write(field):
    async def scenario():
        async with _api(_principal(), [_rule(cameras=["cam-a"])]) as (client, sessions):
            async with sessions() as session:
                before = _state(await session.get(AlarmRuleEntity, "rule"))
            response = await client.patch(
                "/api/v1/alarms/rules/rule", json={"enabled": False, field: None}
            )
            assert response.status_code == 422
            async with sessions() as session:
                assert _state(await session.get(AlarmRuleEntity, "rule")) == before
    asyncio.run(scenario())


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_role_gates_even_with_all_sites(role):
    async def scenario():
        async with _api(_principal(role, ("*",)), [_rule()]) as (client, sessions):
            assert (await client.delete("/api/v1/alarms/rules/rule")).status_code == 403
            if role == "viewer":
                assert (await client.post("/api/v1/alarms/rules", json=_payload())).status_code == 403
                assert (await client.patch(
                    "/api/v1/alarms/rules/rule", json={"enabled": False}
                )).status_code == 403
            async with sessions() as session:
                assert (await session.get(AlarmRuleEntity, "rule")).enabled
    asyncio.run(scenario())


@pytest.mark.parametrize("role", ["operator", "admin"])
def test_all_site_identity_writes_tenant_wide_but_not_other_tenant(role):
    async def scenario():
        async with _api(_principal(role, ("*",)), [_rule("other", tenant="tenant-b")]) as (client, sessions):
            created = await client.post("/api/v1/alarms/rules", json=_payload())
            assert created.status_code == 201
            rule_id = created.json()["id"]
            assert (await client.patch(
                f"/api/v1/alarms/rules/{rule_id}", json={"name": "Changed", "camera_ids": []}
            )).status_code == 200
            if role == "admin":
                assert (await client.delete(f"/api/v1/alarms/rules/{rule_id}")).status_code == 204
                assert (await client.delete("/api/v1/alarms/rules/other")).status_code == 404
            assert (await client.post(
                "/api/v1/alarms/rules", json=_payload(tenant_id="tenant-b")
            )).status_code == 404
            assert (await client.patch(
                "/api/v1/alarms/rules/other", json={"name": "Changed"}
            )).status_code == 404
            async with sessions() as session:
                assert (await session.get(AlarmRuleEntity, "other")).name == "Original"
    asyncio.run(scenario())


def test_shared_reads_and_scoped_instances_survive_soft_disable():
    async def scenario():
        async with _api(_principal("admin"), [
            _rule(), _rule("local", ["cam-a"]), _rule("other", tenant="tenant-b"),
        ]) as (client, sessions):
            fixed_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
            async with sessions() as session:
                for alarm_id, site, camera in [("local-alarm", "site-a", "cam-a"), ("remote", "site-b", "cam-b")]:
                    session.add(AlarmInstanceEntity(
                        id=alarm_id, rule_id="local", event_id=alarm_id, tenant_id="tenant-a",
                        site_id=site, camera_id=camera, event_type="motion", severity="high",
                        message="Synthetic", dedupe_key=alarm_id, opened_at=fixed_time, last_event_at=fixed_time,
                    ))
                await session.commit()
            visible = await client.get("/api/v1/alarms/rules")
            assert {row["id"] for row in visible.json()} == {"rule", "local"}
            assert (await client.delete("/api/v1/alarms/rules/local")).status_code == 204
            for action in ("acknowledge", "close"):
                assert (await client.post(f"/api/v1/alarms/local-alarm/{action}")).status_code == 200
                assert (await client.post(f"/api/v1/alarms/remote/{action}")).status_code == 404
            async with sessions() as session:
                assert not (await session.get(AlarmRuleEntity, "local")).enabled
                assert (await session.get(AlarmInstanceEntity, "local-alarm")).rule_id == "local"
                assert (await session.get(AlarmInstanceEntity, "remote")).state == "open"
    asyncio.run(scenario())


def test_original_wildcard_matches_cross_site_events():
    wildcard = RuleSnapshot(
        "rule", "tenant-a", None, "Synthetic", frozenset({"motion"}), frozenset(),
        frozenset(), "high", 60,
    )
    assert rule_matches(wildcard, {
        "tenant_id": "tenant-a", "site_id": "site-b", "camera_id": "cam-b", "event_type": "motion",
    })
    assert not rule_matches(wildcard, {
        "tenant_id": "tenant-b", "site_id": "site-b", "camera_id": "cam-b", "event_type": "motion",
    })


@pytest.mark.parametrize("changes", [
    {"camera_ids": [None]}, {"camera_ids": "cam-a"}, {"camera_ids": [{}]},
    {"camera_ids": ["cam-a"] * 1001}, {"event_types": []},
    {"alarm_severity": "invalid"}, {"cooldown_seconds": -1}, {"name": ""},
])
def test_invalid_patch_values_are_safe(changes):
    async def scenario():
        async with _api(_principal(), [_rule(cameras=["cam-a"])]) as (client, sessions):
            async with sessions() as session:
                before = _state(await session.get(AlarmRuleEntity, "rule"))
            response = await client.patch("/api/v1/alarms/rules/rule", json={"enabled": False, **changes})
            assert response.status_code == 422
            async with sessions() as session:
                assert _state(await session.get(AlarmRuleEntity, "rule")) == before
    asyncio.run(scenario())


@pytest.mark.parametrize("moved_tenant,moved_site", [
    ("tenant-a", "site-b"), ("tenant-b", "site-a"),
])
def test_moved_camera_prevents_existing_rule_mutation(moved_tenant, moved_site):
    async def scenario():
        async with _api(_principal("admin"), [_rule(cameras=["cam-a"])]) as (client, sessions):
            async with sessions() as session:
                camera = await session.get(CameraEntity, "cam-a")
                camera.tenant_id = moved_tenant
                camera.site_id = moved_site
                await session.commit()
                before = _state(await session.get(AlarmRuleEntity, "rule"))
            for method, changes in [("PATCH", {"camera_ids": ["cam-a2"]}), ("DELETE", None)]:
                response = await client.request(method, "/api/v1/alarms/rules/rule", json=changes)
                assert response.status_code == 404
                async with sessions() as session:
                    assert _state(await session.get(AlarmRuleEntity, "rule")) == before
    asyncio.run(scenario())


def test_all_sites_still_requires_rule_camera_site_consistency():
    async def scenario():
        async with _api(_principal("admin", ("*",))) as (client, sessions):
            response = await client.post(
                "/api/v1/alarms/rules", json=_payload(site_id="site-a", camera_ids=["cam-b"])
            )
            assert response.status_code == 422
            async with sessions() as session:
                assert not (await session.execute(select(AlarmRuleEntity))).scalars().all()
    asyncio.run(scenario())


def test_site_specific_filter_may_be_cleared_without_widening_site():
    async def scenario():
        async with _api(_principal(), [_rule(cameras=["cam-a"], site="site-a")]) as (client, sessions):
            response = await client.patch("/api/v1/alarms/rules/rule", json={"camera_ids": []})
            assert response.status_code == 200
            async with sessions() as session:
                row = await session.get(AlarmRuleEntity, "rule")
                assert row.site_id == "site-a" and row.camera_ids_json == []
    asyncio.run(scenario())


@pytest.mark.parametrize("cameras", [["cam-other"], ["missing"], ["cam-a"] * 1001, [None]])
def test_all_site_identity_fails_closed_on_invalid_existing_references(cameras):
    async def scenario():
        async with _api(_principal("admin", ("*",)), [_rule(cameras=cameras)]) as (client, sessions):
            async with sessions() as session:
                before = _state(await session.get(AlarmRuleEntity, "rule"))
            response = await client.delete("/api/v1/alarms/rules/rule")
            malformed = len(cameras) > 1000 or any(not isinstance(camera, str) for camera in cameras)
            assert response.status_code == (422 if malformed else 404)
            assert response.json() == {"detail": (
                "Invalid alarm-rule camera filter" if malformed else "Resource not found")}
            async with sessions() as session:
                assert _state(await session.get(AlarmRuleEntity, "rule")) == before
    asyncio.run(scenario())
