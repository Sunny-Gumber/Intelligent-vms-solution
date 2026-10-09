"""VMS-FIX-042 residual: a NULL heartbeat_at is older than any observation.

The production column stays NOT NULL (migration 0007). This test builds a
disposable sqlite table without that constraint, which is the case the
residual describes: if the constraint is ever removed, the first observation
must be stored. Without ``OR heartbeat_at IS NULL``, the conditional update
matches nothing, the NULL remains, and NodeRead raises ValidationError.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.auth import Principal
from app.db.base import Base
from app.models.placement import InfrastructureNodeEntity
from app.models.placement_schemas import NodeHeartbeat
from app.routers.placement import heartbeat_node

NODE_ID = "fix042-null-heartbeat"


def _utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _admin():
    return Principal("admin", frozenset({"admin"}), "*", frozenset({"*"}))


async def _null_heartbeat_accepts_first_observation(tmp_path: Path) -> None:
    column = InfrastructureNodeEntity.__table__.c.heartbeat_at
    original_nullable = column.nullable
    column.nullable = True
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fix042-null.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    observed = datetime.now(timezone.utc) - timedelta(seconds=2)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add(
                InfrastructureNodeEntity(
                    id=NODE_ID,
                    name="null-heartbeat",
                    region_id="region-a",
                    roles_json=["media"],
                    state="active",
                    enabled=True,
                    endpoints_json={},
                    capacity_json={},
                    load_json={"cpu": 0.0},
                    heartbeat_at=observed - timedelta(hours=1),
                    authority_mode="central_online",
                )
            )
            await session.commit()
        async with engine.begin() as connection:
            cleared = await connection.execute(
                text("UPDATE infrastructure_nodes SET heartbeat_at = NULL WHERE id = :id"),
                {"id": NODE_ID},
            )
            assert cleared.rowcount == 1
        async with factory() as session:
            stored = await session.get(InfrastructureNodeEntity, NODE_ID)
            assert stored is not None
            assert stored.heartbeat_at is None
            accepted = await heartbeat_node(
                NODE_ID,
                NodeHeartbeat(
                    load={"cpu": 3.0, "active_sources": 0.0},
                    role_readiness={"media": "ready"},
                    authority_mode="regional_autonomous",
                    observed_at=observed,
                ),
                session,
                _admin(),
            )
        assert _utc(accepted.heartbeat_at) == _utc(observed)
        assert float(accepted.load["cpu"]) == 3.0
        assert accepted.role_readiness == {"media": "ready"}
        assert accepted.authority_mode == "regional_autonomous"
        async with factory() as session:
            rejected = await heartbeat_node(
                NODE_ID,
                NodeHeartbeat(
                    load={"cpu": 9.0, "active_sources": 9.0},
                    role_readiness={"media": "not_ready"},
                    authority_mode="central_online",
                    observed_at=observed - timedelta(seconds=30),
                ),
                session,
                _admin(),
            )
            row = await session.get(InfrastructureNodeEntity, NODE_ID)
        assert _utc(rejected.heartbeat_at) == _utc(observed)
        assert float(rejected.load["cpu"]) == 3.0
        assert rejected.role_readiness == {"media": "ready"}
        assert rejected.authority_mode == "regional_autonomous"
        assert row is not None
        assert _utc(row.heartbeat_at) == _utc(observed)
        assert float(dict(row.load_json)["cpu"]) == 3.0
        assert row.role_readiness_json == {"media": "ready"}
        assert row.authority_mode == "regional_autonomous"
    finally:
        column.nullable = original_nullable
        await engine.dispose()


def test_null_heartbeat_is_older_than_the_first_observation(tmp_path):
    """A missing heartbeat accepts the first observation and then keeps order."""
    asyncio.run(_null_heartbeat_accepts_first_observation(tmp_path))
