"""Independent-session HTTP transaction tests on SQLite and the QA PostgreSQL DB.

Default execution uses a fresh file database per case. Independent QA explicitly
sets VMS_ALARM_TRANSACTION_TEST_DATABASE_URL to its disposable PostgreSQL service;
there is no skip or fallback when that supplied database is unavailable.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.auth import Principal, get_principal
from app.core.config import settings
from app.db.base import Base
from app.db.session import get_session
from app.models.entities import AlarmRuleEntity, CameraEntity
from app.routers import alarms


MUTATIONS = [
    ("PATCH", {"name": "Restricted mutation"}),
    ("PATCH", {"enabled": False}),
    ("DELETE", None),
    ("PATCH", {"camera_ids": ["cam-a2"]}),
]


@asynccontextmanager
async def api(tmp_path):
    url = os.environ.get("VMS_ALARM_TRANSACTION_TEST_DATABASE_URL")
    url = url or f"sqlite+aiosqlite:///{tmp_path / 'transactions.db'}"
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    used_sessions = []
    app = FastAPI()
    app.include_router(alarms.router)

    async def identity(request: Request):
        actor = request.headers.get("x-actor", "site")
        return Principal(actor, frozenset({"admin"}), "tenant-a",
                         frozenset({"*"} if actor == "all" else {"site-a"}))

    async def dependency():
        async with sessions() as session:
            used_sessions.append(session)
            yield session

    app.dependency_overrides[get_principal] = identity
    app.dependency_overrides[get_session] = dependency
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            for identifier in ("cam-a", "cam-a2"):
                session.add(CameraEntity(
                    id=identifier, tenant_id="tenant-a", site_id="site-a",
                    name=identifier, host="192.0.2.1", main_path="/synthetic",
                    stream_key=identifier,
                ))
            for identifier in ("rule", "other"):
                session.add(AlarmRuleEntity(
                    id=identifier, tenant_id="tenant-a", site_id=None,
                    name="Original", enabled=True, event_types_json=["motion"],
                    camera_ids_json=["cam-a"], severities_json=[],
                    alarm_severity="high", cooldown_seconds=60,
                ))
            await session.commit()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            yield client, sessions, used_sessions
        assert all(not session.in_transaction() for session in used_sessions)
    finally:
        await engine.dispose()


async def state(sessions, identifier="rule"):
    async with sessions() as session:
        row = await session.get(AlarmRuleEntity, identifier)
        return {column.name: getattr(row, column.name) for column in row.__table__.columns}


@pytest.fixture(autouse=True)
def enable_alarms(monkeypatch):
    monkeypatch.setattr(settings, "alarm_processing_enabled", True)


def pause_first_write(monkeypatch):
    original = alarms._persist_rule_change
    ready, resume = asyncio.Event(), asyncio.Event()
    first = True

    async def paused(session, row, changes):
        nonlocal first
        if first:
            first = False
            ready.set()
            await resume.wait()
        return await original(session, row, changes)

    monkeypatch.setattr(alarms, "_persist_rule_change", paused)
    return ready, resume


@pytest.mark.parametrize("method,changes", MUTATIONS)
@pytest.mark.parametrize("competitor", [{"camera_ids": []}, {"name": "Other writer"}])
def test_changed_rule_conflicts_without_partial_write(tmp_path, monkeypatch, method, changes, competitor):
    async def scenario():
        async with api(tmp_path) as (client, sessions, used):
            ready, resume = pause_first_write(monkeypatch)
            task = asyncio.create_task(client.request(method, "/api/v1/alarms/rules/rule", json=changes))
            try:
                await asyncio.wait_for(ready.wait(), 5)
                winner = await asyncio.wait_for(client.patch(
                    "/api/v1/alarms/rules/rule", json=competitor,
                    headers={"X-Actor": "all"}), 5)
                assert winner.status_code == 200, winner.text
                after_winner = await state(sessions)
                resume.set()
                loser = await asyncio.wait_for(task, 5)
                assert loser.status_code == 409, loser.text
                assert await state(sessions) == after_winner
                assert not used[0].in_transaction()
                retry = await client.request(method, "/api/v1/alarms/rules/rule", json=changes)
                if competitor == {"camera_ids": []}:
                    assert retry.status_code == 404
                    assert await state(sessions) == after_winner
                else:
                    assert retry.status_code == (204 if method == "DELETE" else 200)
                print("TRANSACTION_RACE_EVIDENCE", method, changes, competitor,
                      "winner=200 stale=409 retry=", retry.status_code)
            finally:
                resume.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("method,changes", MUTATIONS)
def test_permitted_mutations_persist(tmp_path, method, changes):
    async def scenario():
        async with api(tmp_path) as (client, sessions, _):
            response = await client.request(method, "/api/v1/alarms/rules/rule", json=changes)
            assert response.status_code == (204 if method == "DELETE" else 200), response.text
            final = await state(sessions)
            assert final["tenant_id"] == "tenant-a" and final["site_id"] is None
            assert final["name"] == (changes or {}).get("name", "Original")
            assert final["enabled"] == (False if method == "DELETE" else (changes or {}).get("enabled", True))
            assert final["camera_ids_json"] == (changes or {}).get("camera_ids", ["cam-a"])
    asyncio.run(scenario())


def test_unrelated_rule_write_does_not_conflict(tmp_path, monkeypatch):
    async def scenario():
        async with api(tmp_path) as (client, sessions, _):
            ready, resume = pause_first_write(monkeypatch)
            task = asyncio.create_task(client.patch("/api/v1/alarms/rules/rule", json={"name": "First"}))
            try:
                await asyncio.wait_for(ready.wait(), 5)
                other = await asyncio.wait_for(client.patch("/api/v1/alarms/rules/other", json={"name": "Second"}), 5)
                assert other.status_code == 200
                resume.set()
                assert (await asyncio.wait_for(task, 5)).status_code == 200
                assert (await state(sessions))["name"] == "First"
                assert (await state(sessions, "other"))["name"] == "Second"
            finally:
                resume.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_cancelled_request_closes_transaction_and_preserves_row(tmp_path, monkeypatch):
    async def scenario():
        async with api(tmp_path) as (client, sessions, used):
            before = await state(sessions)
            ready, resume = pause_first_write(monkeypatch)
            task = asyncio.create_task(client.delete("/api/v1/alarms/rules/rule"))
            try:
                await asyncio.wait_for(ready.wait(), 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert not used[0].in_transaction()
                assert await state(sessions) == before
                next_write = await client.patch("/api/v1/alarms/rules/rule", json={"name": "After cancel"})
                assert next_write.status_code == 200
            finally:
                resume.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_revision_advances_when_clock_repeats(tmp_path, monkeypatch):
    async def scenario():
        async with api(tmp_path) as (client, sessions, _):
            before = await state(sessions)

            class FrozenClock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return before["updated_at"].replace(tzinfo=timezone.utc)

            monkeypatch.setattr(alarms, "datetime", FrozenClock)
            for name in ("First", "Second"):
                response = await client.patch("/api/v1/alarms/rules/rule", json={"name": name})
                assert response.status_code == 200, response.text
                after = await state(sessions)
                assert after["updated_at"] > before["updated_at"]
                before = after
    asyncio.run(scenario())
