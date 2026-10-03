import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.entities import (
    CameraAIPolicyEntity,
    CameraEntity,
    CameraHealthStateEntity,
    EventOutboxEntity,
    RecordingHealthStateEntity,
    RecordingPolicyEntity,
)
from app.models.placement import InfrastructureNodeEntity, PlacementAssignmentEntity
from app.services import operational_metrics


FIXED_NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


async def _snapshot(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    monkeypatch.setattr(operational_metrics, "SessionLocal", session_factory)
    monkeypatch.setattr(operational_metrics.settings, "health_heartbeat_seconds", 300)
    monkeypatch.setattr(operational_metrics.settings, "placement_node_stale_seconds", 30.0)
    monkeypatch.setattr(operational_metrics.settings, "placement_headroom", 0.75)

    camera_online = CameraEntity(
        id="camera-online",
        tenant_id="tenant-a",
        site_id="site-a",
        name="Online camera",
        host="10.0.0.10",
        rtsp_port=554,
        main_path="/main",
        sub_path="/sub",
        stream_key="camera-online-stream",
        media_node_id="node-a",
        enabled=True,
        desired_state="provisioned",
    )
    camera_gap = CameraEntity(
        id="camera-gap",
        tenant_id="tenant-a",
        site_id="site-a",
        name="Gap camera",
        host="10.0.0.11",
        rtsp_port=554,
        main_path="/main",
        sub_path="/sub",
        stream_key="camera-gap-stream",
        media_node_id="node-a",
        enabled=True,
        desired_state="provisioned",
    )
    camera_untracked = CameraEntity(
        id="camera-untracked",
        tenant_id="tenant-a",
        site_id="site-a",
        name="Untracked camera",
        host="10.0.0.12",
        rtsp_port=554,
        main_path="/main",
        sub_path="/sub",
        stream_key="camera-untracked-stream",
        media_node_id="node-a",
        enabled=True,
        desired_state="provisioned",
    )

    async with session_factory() as session:
        async with session.begin():
            session.add_all(
                [
                    camera_online,
                    camera_gap,
                    camera_untracked,
                    CameraHealthStateEntity(
                        camera_id="camera-online",
                        state="online",
                        path_present=True,
                        ready=True,
                        failure_count=0,
                        success_count=2,
                        detail_json={},
                        observed_at=FIXED_NOW - timedelta(seconds=30),
                        changed_at=FIXED_NOW - timedelta(minutes=10),
                    ),
                    CameraHealthStateEntity(
                        camera_id="camera-gap",
                        state="offline",
                        path_present=False,
                        ready=False,
                        failure_count=3,
                        success_count=0,
                        detail_json={},
                        observed_at=FIXED_NOW - timedelta(minutes=20),
                        changed_at=FIXED_NOW - timedelta(minutes=5),
                    ),
                    RecordingPolicyEntity(
                        id="policy-online",
                        camera_id="camera-online",
                        mode="continuous",
                        enabled=True,
                        record_stream_key="camera-online-record",
                        recording_node_id="node-a",
                        retention_days=7,
                        part_duration_ms=1000,
                        segment_duration_seconds=900,
                        max_part_size_mb=50,
                    ),
                    RecordingPolicyEntity(
                        id="policy-gap",
                        camera_id="camera-gap",
                        mode="continuous",
                        enabled=True,
                        record_stream_key="camera-gap-record",
                        recording_node_id="node-a",
                        retention_days=7,
                        part_duration_ms=1000,
                        segment_duration_seconds=900,
                        max_part_size_mb=50,
                    ),
                    RecordingPolicyEntity(
                        id="policy-untracked",
                        camera_id="camera-untracked",
                        mode="continuous",
                        enabled=True,
                        record_stream_key="camera-untracked-record",
                        recording_node_id="node-a",
                        retention_days=7,
                        part_duration_ms=1000,
                        segment_duration_seconds=900,
                        max_part_size_mb=50,
                    ),
                    RecordingHealthStateEntity(
                        camera_id="camera-online",
                        last_segment_id="segment-online",
                        last_segment_completed_at=FIXED_NOW - timedelta(minutes=5),
                        gap_deadline_at=FIXED_NOW + timedelta(minutes=25),
                        recording_node_id="node-a",
                        assignment_generation=2,
                        observed_at=FIXED_NOW - timedelta(minutes=5),
                    ),
                    RecordingHealthStateEntity(
                        camera_id="camera-gap",
                        last_segment_id="segment-gap",
                        last_segment_completed_at=FIXED_NOW - timedelta(hours=1),
                        gap_deadline_at=FIXED_NOW - timedelta(minutes=30),
                        recording_node_id="node-a",
                        assignment_generation=1,
                        observed_at=FIXED_NOW - timedelta(hours=1),
                    ),
                    InfrastructureNodeEntity(
                        id="node-a",
                        name="Node A",
                        region_id="region-a",
                        roles_json=["media", "recording"],
                        state="active",
                        enabled=True,
                        endpoints_json={},
                        capacity_json={
                            "max_sources": 10,
                            "max_recordings": 10,
                        },
                        load_json={
                            "active_sources": 8,
                            "active_recordings": 4,
                        },
                        heartbeat_at=FIXED_NOW - timedelta(seconds=5),
                        authority_mode="central_online",
                        generation=1,
                    ),
                    InfrastructureNodeEntity(
                        id="node-stale",
                        name="Node Stale",
                        region_id="region-a",
                        roles_json=["ai"],
                        state="active",
                        enabled=True,
                        endpoints_json={},
                        capacity_json={"max_ai_jobs": 10},
                        load_json={},
                        heartbeat_at=FIXED_NOW - timedelta(minutes=5),
                        authority_mode="fenced_degraded",
                        generation=1,
                    ),
                    PlacementAssignmentEntity(
                        id="assignment-recording",
                        camera_id="camera-online",
                        role="recording",
                        region_id="region-a",
                        node_id="node-a",
                        cleanup_node_ids_json=[],
                        generation=2,
                        applied_generation=1,
                        active=True,
                        reason="failover",
                        lease_expires_at=FIXED_NOW + timedelta(minutes=1),
                        autonomy_expires_at=None,
                        assigned_at=FIXED_NOW - timedelta(minutes=1),
                    ),
                    CameraAIPolicyEntity(
                        id="ai-policy-1",
                        camera_id="camera-online",
                        enabled=True,
                        source_mode="camera_metadata",
                        model_id=None,
                        stream_role="sub",
                        sample_fps=1.0,
                        min_confidence=0.5,
                        analytics_json=["motion"],
                        zones_json=[],
                        provider_config_json={},
                    ),
                    EventOutboxEntity(
                        id="event:old",
                        topic="vms.events.v1",
                        key_text="tenant-a:camera-online",
                        payload_json={"event_id": "old"},
                        status="pending",
                        attempts=0,
                        next_attempt_at=FIXED_NOW,
                        created_at=FIXED_NOW - timedelta(seconds=75),
                        updated_at=FIXED_NOW - timedelta(seconds=75),
                    ),
                ]
            )

    snapshot = await operational_metrics.collect_operational_snapshot(now=FIXED_NOW)
    await engine.dispose()
    return snapshot


def test_operational_snapshot_uses_bounded_durable_aggregates(monkeypatch):
    """Aggregate camera, recording, node, placement, AI and outbox state deterministically."""
    snapshot = asyncio.run(_snapshot(monkeypatch))

    assert snapshot.camera_states["online"] == 1
    assert snapshot.camera_states["offline"] == 1
    assert snapshot.camera_states["other"] == 0
    assert snapshot.camera_transport_ready == 1
    assert snapshot.media_paths_present == 1
    assert snapshot.camera_health_stale == 1

    assert snapshot.recording_active == 3
    assert snapshot.recording_gap_candidates == 1
    assert snapshot.recording_health_untracked == 1

    assert snapshot.node_states["active"] == 2
    assert snapshot.node_authority_modes["central_online"] == 1
    assert snapshot.node_authority_modes["fenced_degraded"] == 1
    assert snapshot.node_stale == 1
    assert snapshot.node_saturated["media"] == 1
    assert snapshot.node_capacity_unmeasured["ai"] == 1

    assert snapshot.placement_active["recording"] == 1
    assert snapshot.placement_unapplied["recording"] == 1
    assert snapshot.ai_policies_enabled == 1
    assert snapshot.outbox_oldest_pending_age_seconds == 75.0
