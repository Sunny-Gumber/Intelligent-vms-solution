from datetime import timedelta

from tests.time_control import FIXED_NOW

from app.services.placement import NodeSnapshot, choose_node, node_eligible, role_utilization


def node(
    node_id: str,
    *,
    region="r1",
    roles=frozenset({"media"}),
    state="active",
    age=0,
    capacity=None,
    load=None,
):
    return NodeSnapshot(
        id=node_id,
        region_id=region,
        roles=roles,
        state=state,
        enabled=True,
        capacity=capacity if capacity is not None else {"max_ingress_mbps":1000,"max_egress_mbps":1000,"max_sources":1000},
        load=load if load is not None else {"ingress_mbps":100,"egress_mbps":50,"active_sources":100},
        heartbeat_at=FIXED_NOW-timedelta(seconds=age),
    )


def test_media_utilization_uses_worst_dimension():
    util, active = role_utilization(node("a"), "media")
    assert round(util, 3) == 0.1
    assert active == 100


def test_wrong_region_or_role_is_not_eligible():
    now=FIXED_NOW
    assert not node_eligible(node("a",region="r2"),"media","r1",now)
    assert not node_eligible(node("a",roles=frozenset({"recording"})),"media","r1",now)


def test_choose_node_is_deterministic_and_prefers_lower_utilization():
    now=FIXED_NOW
    high=node("b",load={"ingress_mbps":600,"egress_mbps":100,"active_sources":600})
    low=node("a",load={"ingress_mbps":100,"egress_mbps":100,"active_sources":100})
    assert choose_node([high,low],"media","r1",now).id == "a"
    assert choose_node([low,high],"media","r1",now).id == "a"


def test_zero_capacity_node_is_not_eligible():
    now=FIXED_NOW
    empty=node("a",capacity={})
    assert not node_eligible(empty,"media","r1",now)


def test_stale_node_is_not_eligible():
    now = FIXED_NOW
    assert not node_eligible(node("stale", age=120), "media", "r1", now)


def test_headroom_blocks_overloaded_node():
    now = FIXED_NOW
    overloaded = node(
        "full",
        load={"ingress_mbps":800, "egress_mbps":100, "active_sources":800},
    )
    assert not node_eligible(overloaded, "media", "r1", now)


def test_projected_assignments_spread_fresh_batch():
    from app.services.placement import project_assignment

    now = FIXED_NOW
    left = node("a", capacity={"max_sources": 2}, load={"active_sources": 0})
    right = node("b", capacity={"max_sources": 2}, load={"active_sources": 0})
    first = choose_node([left, right], "media", "r1", now)
    assert first.id == "a"
    project_assignment(first, "media")
    second = choose_node([left, right], "media", "r1", now)
    assert second.id == "b"


def test_repeated_failover_keeps_all_stale_nodes():
    import asyncio

    from app.models.entities import CameraEntity
    from app.models.placement import PlacementAssignmentEntity
    from app.services.placement import _assign

    camera = CameraEntity(
        id="cam-1",
        tenant_id="t",
        site_id="s",
        name="camera",
        host="10.0.0.10",
        rtsp_port=554,
        main_path="/main",
        stream_key="cam-1",
        media_node_id="A",
        desired_state="provisioned",
    )
    row = PlacementAssignmentEntity(
        camera_id="cam-1",
        role="media",
        region_id="r1",
        node_id="A",
        cleanup_node_ids_json=[],
        generation=1,
        active=True,
        reason="initial",
        lease_expires_at=FIXED_NOW + timedelta(seconds=60),
        assigned_at=FIXED_NOW,
    )

    row, changed = asyncio.run(_assign(None, camera, "media", "r1", node("B"), row, reason="failover", now=FIXED_NOW))
    assert changed
    assert row.cleanup_node_ids_json == ["A"]

    row, changed = asyncio.run(_assign(None, camera, "media", "r1", node("C"), row, reason="failover", now=FIXED_NOW))
    assert changed
    assert row.cleanup_node_ids_json == ["A", "B"]


def test_failback_removes_new_current_node_from_cleanup():
    import asyncio

    from app.models.entities import CameraEntity
    from app.models.placement import PlacementAssignmentEntity
    from app.services.placement import _assign

    camera = CameraEntity(
        id="cam-1",
        tenant_id="t",
        site_id="s",
        name="camera",
        host="10.0.0.10",
        rtsp_port=554,
        main_path="/main",
        stream_key="cam-1",
        media_node_id="B",
        desired_state="assigned",
    )
    row = PlacementAssignmentEntity(
        camera_id="cam-1",
        role="media",
        region_id="r1",
        node_id="B",
        cleanup_node_ids_json=["A"],
        generation=2,
        active=True,
        reason="failover",
        lease_expires_at=FIXED_NOW + timedelta(seconds=60),
        assigned_at=FIXED_NOW,
    )

    row, changed = asyncio.run(_assign(None, camera, "media", "r1", node("A"), row, reason="failover", now=FIXED_NOW))
    assert changed
    assert row.cleanup_node_ids_json == ["B"]


def test_missing_configured_load_metric_makes_node_ineligible():
    now = FIXED_NOW
    recording = node(
        "rec-a",
        roles=frozenset({"recording"}),
        capacity={"max_record_mbps": 1000, "max_recordings": 100},
        load={"active_recordings": 10},
    )
    assert not node_eligible(recording, "recording", "r1", now)


def test_zero_capacity_dimension_can_be_explicitly_disabled():
    now = FIXED_NOW
    recording = node(
        "rec-a",
        roles=frozenset({"recording"}),
        capacity={"max_record_mbps": 0, "max_recordings": 100},
        load={"active_recordings": 10},
    )
    assert node_eligible(recording, "recording", "r1", now)


def test_ai_node_without_scheduler_load_is_not_eligible():
    now = FIXED_NOW
    ai = node(
        "ai-a",
        roles=frozenset({"ai"}),
        capacity={"max_ai_mpix_s": 100, "max_ai_jobs": 10},
        load={"host_cpu_utilization_pct": 20},
    )
    assert not node_eligible(ai, "ai", "r1", now)


def test_region_metadata_change_does_not_rotate_ownership_generation():
    import asyncio

    from app.models.entities import CameraEntity
    from app.models.placement import PlacementAssignmentEntity
    from app.services.placement import _assign

    camera = CameraEntity(
        id="cam-region",
        tenant_id="t",
        site_id="s",
        name="camera",
        host="10.0.0.10",
        rtsp_port=554,
        main_path="/main",
        stream_key="cam-region",
        media_node_id="node-a",
        desired_state="provisioned",
    )
    row = PlacementAssignmentEntity(
        id="pa-region",
        camera_id=camera.id,
        role="media",
        region_id="old-region",
        node_id="node-a",
        cleanup_node_ids_json=[],
        generation=5,
        applied_generation=5,
        active=True,
        reason="initial",
        lease_expires_at=FIXED_NOW + timedelta(seconds=60),
        assigned_at=FIXED_NOW,
    )
    target = node("node-a", region="new-region")

    updated, changed = asyncio.run(
        _assign(None, camera, "media", "new-region", target, row, reason="region-change", now=FIXED_NOW)
    )

    assert changed
    assert updated.generation == 5
    assert updated.applied_generation == 5
    assert updated.region_id == "new-region"


def test_node_change_rotates_generation_and_clears_applied_generation():
    import asyncio

    from app.models.entities import CameraEntity
    from app.models.placement import PlacementAssignmentEntity
    from app.services.placement import _assign

    camera = CameraEntity(
        id="cam-owner",
        tenant_id="t",
        site_id="s",
        name="camera",
        host="10.0.0.10",
        rtsp_port=554,
        main_path="/main",
        stream_key="cam-owner",
        media_node_id="node-a",
        desired_state="provisioned",
    )
    row = PlacementAssignmentEntity(
        id="pa-owner",
        camera_id=camera.id,
        role="media",
        region_id="r1",
        node_id="node-a",
        cleanup_node_ids_json=[],
        generation=5,
        applied_generation=5,
        active=True,
        reason="initial",
        lease_expires_at=FIXED_NOW + timedelta(seconds=60),
        assigned_at=FIXED_NOW,
    )

    updated, changed = asyncio.run(
        _assign(None, camera, "media", "r1", node("node-b"), row, reason="failover", now=FIXED_NOW)
    )

    assert changed
    assert updated.generation == 6
    assert updated.applied_generation is None


def test_regional_autonomous_node_is_not_eligible_for_central_renewal():
    now = FIXED_NOW
    autonomous = node("node-a")
    autonomous = NodeSnapshot(
        id=autonomous.id,
        region_id=autonomous.region_id,
        roles=autonomous.roles,
        state=autonomous.state,
        enabled=autonomous.enabled,
        capacity=autonomous.capacity,
        load=autonomous.load,
        heartbeat_at=autonomous.heartbeat_at,
        authority_mode="regional_autonomous",
    )
    assert not node_eligible(autonomous, "media", "r1", now)


def test_fenced_degraded_node_is_not_eligible_for_central_renewal():
    now = FIXED_NOW
    degraded = node("node-a")
    degraded = NodeSnapshot(
        id=degraded.id,
        region_id=degraded.region_id,
        roles=degraded.roles,
        state=degraded.state,
        enabled=degraded.enabled,
        capacity=degraded.capacity,
        load=degraded.load,
        heartbeat_at=degraded.heartbeat_at,
        authority_mode="fenced_degraded",
    )
    assert not node_eligible(degraded, "media", "r1", now)
