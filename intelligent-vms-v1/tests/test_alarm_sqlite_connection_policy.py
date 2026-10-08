"""Pinned-stack HTTP checks of physical SQLite pool reuse and alarm-only policy."""

import asyncio
from contextlib import asynccontextmanager
import time

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.sql.dml import Update

from app.core.auth import Principal, get_principal
from app.core.config import settings
from app.db.base import Base
from app.db.session import get_session
from app.models.entities import AlarmRuleEntity, CameraEntity


_PRIOR_TIMEOUT = 9137


@asynccontextmanager
async def _pooled_api(tmp_path, monkeypatch):
    """Use one pooled physical connection with a non-default sentinel timeout."""
    from app.routers.alarms import router
    monkeypatch.setattr(settings, "alarm_processing_enabled", True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pool.db'}",
                                 pool_size=1, max_overflow=0, pool_timeout=2, connect_args={"timeout": 6.321})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    updates = []
    retained_drivers = []

    @event.listens_for(engine.sync_engine, "checkout")
    def retain_driver(connection, record, proxy):
        retained_drivers.append(connection.driver_connection)

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def observe_update(connection, cursor, statement, parameters, context, many):
        if statement.startswith("UPDATE alarm_rules"):
            raw = connection.connection.dbapi_connection
            probe = raw.cursor()
            try:
                probe.execute("PRAGMA busy_timeout")
                updates.append((id(raw.driver_connection), probe.fetchone()[0]))
            finally:
                probe.close()

    app = FastAPI()
    app.include_router(router)

    async def identity():
        return Principal("synthetic-admin", frozenset({"admin"}), "tenant-a", frozenset({"site-a"}))

    async def dependency():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_principal] = identity
    app.dependency_overrides[get_session] = dependency
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(CameraEntity(id="cam", tenant_id="tenant-a", site_id="site-a",
                                     name="Synthetic", host="192.0.2.1", main_path="/test", stream_key="cam"))
            session.add(AlarmRuleEntity(id="rule", tenant_id="tenant-a", site_id=None,
                name="Original", enabled=True, event_types_json=["motion"], camera_ids_json=["cam"],
                severities_json=[], alarm_severity="high", cooldown_seconds=60))
            await session.commit()
        async with engine.connect() as connection:
            await connection.exec_driver_sql(f"PRAGMA busy_timeout = {_PRIOR_TIMEOUT}")
            original_driver = (await connection.get_raw_connection()).driver_connection
            assert (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar_one() == _PRIOR_TIMEOUT
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                     base_url="http://test") as client:
            yield client, engine, sessions, original_driver, updates
    finally:
        await engine.dispose()


async def _assert_reused(engine, original_driver):
    """Perform an unrelated checkout and assert physical identity and actual prior policy."""
    async with engine.connect() as connection:
        driver = (await connection.get_raw_connection()).driver_connection
        timeout = (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar_one()
        assert driver is original_driver, "Expected the same physical SQLite connection"
        assert timeout == _PRIOR_TIMEOUT, (timeout, _PRIOR_TIMEOUT)
        assert (await connection.execute(text("SELECT 42"))).scalar_one() == 42
        print("SQLITE_POOL_REUSE", id(driver), "restored_timeout=", timeout)


@pytest.mark.parametrize("outcome", ["success", "conflict", "error", "cancel", "busy"])
def test_alarm_policy_restores_same_physical_connection(tmp_path, monkeypatch, outcome):
    """Verify success, predicate conflict, exception, cancellation and real writer contention."""
    async def scenario():
        async with _pooled_api(tmp_path, monkeypatch) as (client, engine, sessions, driver, updates):
            original_execute = AsyncSession.execute
            entered, resume = asyncio.Event(), asyncio.Event()

            async def intercepted(session, statement, *args, **kwargs):
                if isinstance(statement, Update):
                    if outcome == "error":
                        raise RuntimeError("synthetic pre-write failure")
                    if outcome in {"cancel", "busy"}:
                        entered.set()
                        await resume.wait()
                return await original_execute(session, statement, *args, **kwargs)

            monkeypatch.setattr(AsyncSession, "execute", intercepted)
            task = asyncio.create_task(client.patch("/api/v1/alarms/rules/rule", json={"name": "Changed"}))
            holder_engine = None
            holder = None
            try:
                if outcome == "conflict":
                    # Compare a changed revision without starting another pool checkout.
                    from app.routers import alarms
                    original_persist = alarms._persist_rule_change
                    async def stale(session, row, changes):
                        row.updated_at = row.updated_at.replace(year=2001)
                        session.expunge(row)
                        return await original_persist(session, row, changes)
                    monkeypatch.setattr(alarms, "_persist_rule_change", stale)
                if outcome in {"cancel", "busy"}:
                    await asyncio.wait_for(entered.wait(), 3)
                    if outcome == "cancel":
                        task.cancel()
                    else:
                        holder_engine = create_async_engine(engine.url)
                        holder = await holder_engine.connect()
                        await holder.execute(update(AlarmRuleEntity).where(
                            AlarmRuleEntity.id == "rule").values(name="Uncommitted"))
                        started = time.monotonic()
                        resume.set()
                if outcome == "cancel":
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 6)
                else:
                    response = await asyncio.wait_for(task, 6)
                    assert response.status_code == {"success": 200, "conflict": 409, "error": 500, "busy": 409}[outcome]
                    if outcome == "busy":
                        assert 1 <= time.monotonic() - started < 6
                        print("SQLITE_BUSY_BOUND", time.monotonic() - started)
                if holder is not None:
                    await holder.rollback()
                    await holder.close()
                    holder = None
                await _assert_reused(engine, driver)
                if updates:
                    assert all(identity == id(driver) and timeout == 2000 for identity, timeout in updates)
                async with sessions() as session:
                    row = await session.get(AlarmRuleEntity, "rule")
                    assert row.name == ("Changed" if outcome == "success" else "Original")
                    assert row.enabled and row.camera_ids_json == ["cam"]
                monkeypatch.setattr(AsyncSession, "execute", original_execute)
                if outcome != "conflict":
                    assert (await client.patch("/api/v1/alarms/rules/rule", json={"name": "Reuse"})).status_code == 200
                    await _assert_reused(engine, driver)
            finally:
                resume.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if holder is not None:
                    await holder.rollback()
                    await holder.close()
                if holder_engine is not None:
                    await holder_engine.dispose()
    asyncio.run(scenario())


def test_repeated_cancellation_cannot_return_connection_before_restoration(tmp_path, monkeypatch):
    """Cancel a request again while its shielded same-connection cleanup is paused."""
    async def scenario():
        async with _pooled_api(tmp_path, monkeypatch) as (client, engine, sessions, driver, _):
            entered, restoring, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
            original_execute = AsyncSession.execute
            original_sql = AsyncConnection.exec_driver_sql

            async def paused_write(session, statement, *args, **kwargs):
                if isinstance(statement, Update):
                    entered.set()
                    await asyncio.Event().wait()
                return await original_execute(session, statement, *args, **kwargs)

            async def paused_restore(connection, statement, *args, **kwargs):
                if statement == f"PRAGMA busy_timeout = {_PRIOR_TIMEOUT}":
                    assert (await connection.get_raw_connection()).driver_connection is driver
                    restoring.set()
                    await release.wait()
                return await original_sql(connection, statement, *args, **kwargs)

            monkeypatch.setattr(AsyncSession, "execute", paused_write)
            monkeypatch.setattr(AsyncConnection, "exec_driver_sql", paused_restore)
            task = asyncio.create_task(client.delete("/api/v1/alarms/rules/rule"))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                task.cancel()
                await asyncio.wait_for(restoring.wait(), 3)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                assert engine.pool.checkedout() == 1
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
                await _assert_reused(engine, driver)
                async with sessions() as session:
                    assert (await session.get(AlarmRuleEntity, "rule")).enabled
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_restoration_failure_invalidates_instead_of_returning_dirty_connection(tmp_path, monkeypatch):
    """Fail restoration after a conflict and prove the next checkout is a clean replacement."""
    async def scenario():
        async with _pooled_api(tmp_path, monkeypatch) as (client, engine, sessions, driver, _):
            from app.routers import alarms
            original_persist = alarms._persist_rule_change
            original_sql = AsyncConnection.exec_driver_sql
            invalidated = []

            @event.listens_for(engine.sync_engine, "invalidate")
            def invalidation(connection, record, error):
                invalidated.append(connection.driver_connection)

            async def stale(session, row, changes):
                row.updated_at = row.updated_at.replace(year=2001)
                session.expunge(row)
                return await original_persist(session, row, changes)

            async def failed_restore(connection, statement, *args, **kwargs):
                if statement == f"PRAGMA busy_timeout = {_PRIOR_TIMEOUT}":
                    raise RuntimeError("synthetic restoration failure")
                return await original_sql(connection, statement, *args, **kwargs)

            monkeypatch.setattr(alarms, "_persist_rule_change", stale)
            monkeypatch.setattr(AsyncConnection, "exec_driver_sql", failed_restore)
            result = await client.delete("/api/v1/alarms/rules/rule")
            assert result.status_code == 500
            assert invalidated == [driver]
            async with engine.connect() as connection:
                replacement = (await connection.get_raw_connection()).driver_connection
                assert replacement is not driver
                assert (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar_one() == 6321
            async with sessions() as session:
                row = await session.get(AlarmRuleEntity, "rule")
                assert row.enabled and row.name == "Original"
            monkeypatch.setattr(alarms, "_persist_rule_change", original_persist)
            assert (await client.patch("/api/v1/alarms/rules/rule", json={"name": "Clean reuse"})).status_code == 200
            async with engine.connect() as connection:
                assert (await connection.get_raw_connection()).driver_connection is replacement
                assert (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar_one() == 6321
            print("SQLITE_POLICY_FAILURE invalidated_old=True replacement_policy=6321 reuse=200")
    asyncio.run(scenario())


def test_cancel_during_real_sqlite_contention_never_restores_on_fresh_checkout(tmp_path, monkeypatch):
    """Cancel an actual SQLite UPDATE wait, then verify safe original or replacement reuse."""
    async def scenario():
        async with _pooled_api(tmp_path, monkeypatch) as (client, engine, sessions, driver, updates):
            holder_engine = create_async_engine(engine.url)
            try:
                async with holder_engine.connect() as holder:
                    await holder.execute(update(AlarmRuleEntity).where(
                        AlarmRuleEntity.id == "rule").values(name="Uncommitted"))
                    task = asyncio.create_task(client.patch("/api/v1/alarms/rules/rule", json={"name": "Cancelled"}))
                    try:
                        async def wait_for_sql():
                            while not updates:
                                await asyncio.sleep(0.01)
                        await asyncio.wait_for(wait_for_sql(), 1)
                        await asyncio.sleep(0.05)
                        assert not task.done()
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await asyncio.wait_for(task, 6)
                    finally:
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        await holder.rollback()
                async with engine.connect() as connection:
                    reused = (await connection.get_raw_connection()).driver_connection
                    timeout = (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar_one()
                    assert timeout == (_PRIOR_TIMEOUT if reused is driver else 6321)
                    print("SQLITE_WAIT_CANCEL same_physical=", reused is driver, "safe_policy=", timeout)
                async with sessions() as session:
                    assert (await session.get(AlarmRuleEntity, "rule")).name == "Original"
                retry = await client.patch("/api/v1/alarms/rules/rule", json={"name": "After cancellation"})
                assert retry.status_code == 200
            finally:
                await holder_engine.dispose()
    asyncio.run(scenario())
