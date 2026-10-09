"""REV-014-001 and REV-014-003. Fence coverage for camera delete and heartbeat.

Collected only when PostgreSQL accepts connections on 127.0.0.1:5432, the same
probe the lease-renewal tests use. The hosted unit job has no PostgreSQL
service, so these tests are absent there rather than skipped.
"""

from __future__ import annotations

import asyncio
import os
import socket
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.auth import Principal
from app.core.config import settings
from app.db.base import Base
from app.models.entities import CameraEntity
from app.models.placement import InfrastructureNodeEntity, PlacementAssignmentEntity
from app.models.placement_schemas import NodeHeartbeat
from app.routers import cameras, placement
from app.services.coordination import PLACEMENT_EXECUTION_LOCK_KEY


T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
CAMERA_ID = "cam-142"
NODE_ID = "node-142"
ADMIN = Principal(
    "issue-142-operator",
    frozenset({"admin", "operator"}),
    "tenant-a",
    frozenset({"site-a"}),
)
SERVICE = Principal(
    "issue-142-node",
    frozenset({"service"}),
    "*",
    frozenset({"*"}),
    node_id=NODE_ID,
)


def _tcp_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _postgres_urls():
    """PostgreSQL URLs for this file. Empty when nothing is listening."""
    configured = os.environ.get("VMS_PLACEMENT_AUTHORITY_TEST_DATABASE_URL")
    if configured:
        return [configured]
    if _tcp_open("127.0.0.1", 5432):
        return ["postgresql+asyncpg://vms:vms@127.0.0.1:5432/vms_fix_014"]
    return []


async def _open(url: str):
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    return factory, engine


def _node():
    return InfrastructureNodeEntity(
        id=NODE_ID,
        name=NODE_ID,
        region_id="region-a",
        roles_json=["media"],
        state="active",
        enabled=True,
        capacity_json={"max_sources": 10, "max_ingress_mbps": 100, "max_egress_mbps": 100},
        load_json={"active_sources": 1, "ingress_mbps": 1, "egress_mbps": 1},
        heartbeat_at=T0,
        authority_mode="central_online",
    )


def _camera():
    return CameraEntity(
        id=CAMERA_ID,
        tenant_id="tenant-a",
        site_id="site-a",
        name=CAMERA_ID,
        host="192.0.2.142",
        rtsp_port=554,
        main_path="/main",
        stream_key="stream-142",
        media_node_id=NODE_ID,
        enabled=True,
        desired_state="provisioned",
    )


def _assignment():
    return PlacementAssignmentEntity(
        id="pa-142",
        camera_id=CAMERA_ID,
        role="media",
        region_id="region-a",
        node_id=NODE_ID,
        cleanup_node_ids_json=[],
        generation=1,
        applied_generation=1,
        active=True,
        reason="initial",
        lease_expires_at=T0,
        autonomy_expires_at=None,
        assigned_at=T0,
    )


if _postgres_urls():

    @pytest.mark.parametrize("database_url", _postgres_urls())
    def test_disabled_placement_delete_returns_409_while_fence_holds_assignment(
        tmp_path, monkeypatch, database_url
    ):
        """REV-014-001. Placement-disabled delete takes the fence and returns 409.

        A renewal holds the placement advisory lock and the assignment row.
        Delete with placement execution disabled used to skip the fence and
        block on that assignment row. It must return 409 while the holder
        still owns both locks, and it must not remove media paths first.
        After the holder releases, the same delete removes the camera.
        """
        del tmp_path

        async def scenario():
            factory, engine = await _open(database_url)
            deleted_paths: list[str] = []

            async def record_delete(path: str) -> None:
                deleted_paths.append(path)

            previous = settings.placement_execution_enabled
            settings.placement_execution_enabled = False
            monkeypatch.setattr(cameras.mediamtx, "delete_path", record_delete)
            ready = asyncio.Event()
            release = asyncio.Event()
            holder_error: list[BaseException] = []

            async def hold_fence_and_assignment():
                async with factory() as session:
                    trans = await session.begin()
                    try:
                        await session.execute(
                            text("SELECT pg_advisory_xact_lock(:key)"),
                            {"key": PLACEMENT_EXECUTION_LOCK_KEY},
                        )
                        locked = await session.execute(
                            text(
                                "SELECT id FROM placement_assignments "
                                "WHERE camera_id = :camera_id FOR UPDATE"
                            ),
                            {"camera_id": CAMERA_ID},
                        )
                        assert locked.first() is not None
                        ready.set()
                        await release.wait()
                    except Exception as exc:
                        holder_error.append(exc)
                        ready.set()
                        raise
                    finally:
                        await trans.rollback()

            outcome = {"code": None, "error": None}
            delete_task = None
            holder = None
            try:
                async with factory() as session:
                    session.add_all([_node(), _camera(), _assignment()])
                    await session.commit()
                holder = asyncio.create_task(hold_fence_and_assignment())
                await asyncio.wait_for(ready.wait(), 5)
                assert holder_error == []

                async def do_delete():
                    async with factory() as session:
                        try:
                            await cameras.delete_camera(CAMERA_ID, session, ADMIN)
                        except HTTPException as exc:
                            outcome["code"] = exc.status_code
                            outcome["error"] = str(exc.detail)

                delete_task = asyncio.create_task(do_delete())
                try:
                    await asyncio.wait_for(delete_task, 1.5)
                except asyncio.TimeoutError:
                    outcome["code"] = "blocked"
                assert release.is_set() is False
                assert outcome["code"] == 409, outcome
                assert deleted_paths == []
                async with factory() as session:
                    assert await session.get(CameraEntity, CAMERA_ID) is not None

                release.set()
                await asyncio.wait_for(holder, 5)
                holder = None
                async with factory() as session:
                    await cameras.delete_camera(CAMERA_ID, session, ADMIN)
                async with factory() as session:
                    assert await session.get(CameraEntity, CAMERA_ID) is None
                assert deleted_paths == [
                    "stream-142-third",
                    "stream-142-main",
                    "stream-142",
                ]
            finally:
                release.set()
                settings.placement_execution_enabled = previous
                if holder is not None:
                    await asyncio.wait_for(holder, 5)
                if delete_task is not None and not delete_task.done():
                    delete_task.cancel()
                    try:
                        await delete_task
                    except asyncio.CancelledError:
                        pass
                await engine.dispose()

        asyncio.run(scenario())


    @pytest.mark.parametrize("database_url", _postgres_urls())
    def test_heartbeat_waits_for_fence_before_locking_node_row(tmp_path, database_url):
        """REV-014-003. Heartbeat takes the advisory lock before the node row.

        Renewal holds the placement fence, then share-locks the node. A
        heartbeat that locks the node row and then waits for the fence
        deadlocks with that share lock. While the fence is held, this
        heartbeat must still be waiting, the authority mode must be unchanged,
        and a share lock of the node must succeed. After the fence is
        released, the conditional update stores the newer mode.
        """
        del tmp_path

        async def scenario():
            factory, engine = await _open(database_url)
            ready = asyncio.Event()
            release = asyncio.Event()
            holder_error: list[BaseException] = []

            async def hold_fence():
                async with factory() as session:
                    trans = await session.begin()
                    try:
                        await session.execute(
                            text("SELECT pg_advisory_xact_lock(:key)"),
                            {"key": PLACEMENT_EXECUTION_LOCK_KEY},
                        )
                        ready.set()
                        await release.wait()
                    except Exception as exc:
                        holder_error.append(exc)
                        ready.set()
                        raise
                    finally:
                        await trans.rollback()

            heartbeat_task = None
            holder = None
            try:
                async with factory() as session:
                    session.add(_node())
                    await session.commit()
                holder = asyncio.create_task(hold_fence())
                await asyncio.wait_for(ready.wait(), 5)
                assert holder_error == []

                async def send():
                    async with factory() as session:
                        return await placement.heartbeat_node(
                            NODE_ID,
                            NodeHeartbeat(
                                load={"cpu": 1.0},
                                authority_mode="fenced_degraded",
                                observed_at=T0 + timedelta(seconds=30),
                            ),
                            session,
                            SERVICE,
                        )

                heartbeat_task = asyncio.create_task(send())
                await asyncio.sleep(0.4)
                assert heartbeat_task.done() is False, "heartbeat wrote while the fence was held"
                async with factory() as session:
                    stored = await session.get(InfrastructureNodeEntity, NODE_ID)
                    assert stored.authority_mode == "central_online"
                    assert stored.heartbeat_at == T0
                async with factory() as share:
                    async with share.begin():
                        await share.execute(text("SELECT set_config('lock_timeout', '500', true)"))
                        locked = await share.execute(
                            text("SELECT id FROM infrastructure_nodes WHERE id = :id FOR SHARE"),
                            {"id": NODE_ID},
                        )
                        assert locked.scalar_one() == NODE_ID
            finally:
                release.set()
                if holder is not None:
                    await asyncio.wait_for(holder, 5)
                if heartbeat_task is not None:
                    result = await asyncio.wait_for(heartbeat_task, 5)
                else:
                    result = None
                await engine.dispose()

            assert holder_error == []
            assert result is not None
            assert result.authority_mode == "fenced_degraded"
            assert result.heartbeat_at == T0 + timedelta(seconds=30)

        asyncio.run(scenario())
