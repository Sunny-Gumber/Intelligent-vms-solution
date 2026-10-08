"""Explicit PostgreSQL-only gate: real lock timeout, rollback and cancellation.

Run with the independent QA disposable database. Missing PostgreSQL configuration
is an error; this file is explicitly collected by that workflow, with no skips.
"""

import asyncio
import os
import time

import pytest
from sqlalchemy import select

from app.models.entities import AlarmRuleEntity
from tests.test_alarm_write_transactions import api, enable_alarms, state


@pytest.mark.parametrize("method,changes", [
    ("PATCH", {"name": "Blocked"}), ("PATCH", {"enabled": False}), ("DELETE", None),
])
def test_postgres_busy_write_rolls_back_and_retry_succeeds(tmp_path, method, changes):
    assert os.environ["VMS_ALARM_TRANSACTION_TEST_DATABASE_URL"].startswith("postgresql+")

    async def scenario():
        async with api(tmp_path) as (client, sessions, used):
            before = await state(sessions)
            async with sessions() as holder:
                await holder.execute(select(AlarmRuleEntity).where(
                    AlarmRuleEntity.id == "rule").with_for_update())
                started = time.monotonic()
                response = await asyncio.wait_for(client.request(
                    method, "/api/v1/alarms/rules/rule", json=changes), 8)
                elapsed = time.monotonic() - started
                assert response.status_code == 409, response.text
                assert 1 <= elapsed < 8, elapsed
                assert not used[0].in_transaction()
                assert await state(sessions) == before
                # A different rule is writable even while this lock is held.
                other = await asyncio.wait_for(client.patch(
                    "/api/v1/alarms/rules/other", json={"name": "Independent"}), 3)
                assert other.status_code == 200
                await holder.rollback()
            retry = await client.request(method, "/api/v1/alarms/rules/rule", json=changes)
            assert retry.status_code == (204 if method == "DELETE" else 200)
            final = await state(sessions)
            assert final["camera_ids_json"] == ["cam-a"]
            assert final["enabled"] == (False if method == "DELETE" else changes.get("enabled", True))
            assert final["name"] == (changes or {}).get("name", "Original")
            print("POSTGRES_LOCK_EVIDENCE", method, "busy=409 seconds=", elapsed,
                  "retry=", retry.status_code)
    asyncio.run(scenario())


def test_cancel_during_postgres_lock_wait_releases_session(tmp_path):
    assert os.environ["VMS_ALARM_TRANSACTION_TEST_DATABASE_URL"].startswith("postgresql+")

    async def scenario():
        async with api(tmp_path) as (client, sessions, used):
            before = await state(sessions)
            async with sessions() as holder:
                await holder.execute(select(AlarmRuleEntity).where(
                    AlarmRuleEntity.id == "rule").with_for_update())
                task = asyncio.create_task(client.patch(
                    "/api/v1/alarms/rules/rule", json={"name": "Cancelled"}))
                try:
                    # Observe the real PostgreSQL backend lock wait; a scheduling
                    # delay alone does not prove cancellation during SQL execution.
                    from sqlalchemy import text
                    async def wait_for_lock():
                        while True:
                            async with sessions() as observer:
                                waiting = (await observer.execute(text(
                                    "SELECT count(*) FROM pg_stat_activity "
                                    "WHERE datname = current_database() "
                                    "AND wait_event_type = 'Lock' "
                                    "AND query LIKE 'UPDATE alarm_rules%'"
                                ))).scalar_one()
                            if waiting:
                                return
                            await asyncio.sleep(0.01)
                    await asyncio.wait_for(wait_for_lock(), 1.5)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 3)
                    assert not used[0].in_transaction()
                    assert await state(sessions) == before
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await holder.rollback()
            retry = await client.patch("/api/v1/alarms/rules/rule", json={"name": "After cancel"})
            assert retry.status_code == 200
    asyncio.run(scenario())
