"""Independent Category 1 QA regressions for lifecycle and scope failures."""

import asyncio
import base64
import importlib.util
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.core.auth import Principal
from app.core.config import settings
from app.models.entities import CameraEntity
from app.models.placement import InfrastructureNodeEntity, PlacementAssignmentEntity
from app.models.schemas import OnvifOnboardRequest, OnvifQrOnboardRequest, OnvifSerialOnboardRequest
from app.routers import cameras, onvif as onvif_router
from app.services import camera_lifecycle, fencing, reconciler
from app.services.onvif_client import OnvifError, probe_host
from app.services.network_policy import TargetNotAllowed, pin_site_http_xaddr
from app.services.onvif_onboarding import QR_PREFIX, decode_qr_payload
from app.services.stream_keys import make_role_stream_key
from tests.time_control import FIXED_NOW, FrozenDateTime


@pytest.fixture(autouse=True)
def deterministic_clock(monkeypatch):
    """Freeze reconciliation leases for deterministic failure tests."""
    monkeypatch.setattr(reconciler, "datetime", FrozenDateTime)


def _camera():
    return CameraEntity(
        id="qa-camera",
        tenant_id="tenant-a",
        site_id="site-a",
        name="QA Gate",
        host="192.168.1.20",
        rtsp_port=554,
        main_path="/main",
        sub_path="/sub",
        third_path="/third-new",
        third_stream_key="qa-gate-third",
        username_enc=None,
        password_enc=None,
        stream_key="qa-gate",
        media_node_id="media-new",
        enabled=True,
        desired_state="pending-source-refresh",
        created_at=FIXED_NOW,
    )


def _assignment(camera, *, dirty=False, cleanup=()):
    return PlacementAssignmentEntity(
        id="qa-assignment",
        camera_id=camera.id,
        role="media",
        region_id="region-a",
        node_id="media-new",
        generation=4,
        applied_generation=None if dirty else 4,
        active=True,
        reason="qa",
        cleanup_node_ids_json=list(cleanup),
        lease_expires_at=FIXED_NOW + timedelta(seconds=60),
        assigned_at=FIXED_NOW,
    )


def _node(node_id):
    return InfrastructureNodeEntity(
        id=node_id,
        name=node_id,
        region_id="region-a",
        roles_json=["media"],
        endpoints_json={"api_url": f"http://{node_id}:9997"},
        capacity_json={},
        load_json={},
        state="active",
        enabled=True,
        heartbeat_at=FIXED_NOW,
        generation=1,
    )


class _MediaClient:
    def __init__(self, configured=(), *, fail_add=(), fail_delete=()):
        self.configured = set(configured)
        self.added = []
        self.deleted = []
        self.fail_add = set(fail_add)
        self.fail_delete = set(fail_delete)

    async def list_config_paths(self):
        return {
            "items": [
                {"name": key, "maxReaders": settings.live_view_max_readers_per_path}
                for key in sorted(self.configured)
            ]
        }

    async def add_or_replace_path(self, key, source):
        self.added.append((key, source))
        if key in self.fail_add:
            raise RuntimeError("synthetic upstream failure")
        self.configured.add(key)

    async def delete_path(self, key):
        self.deleted.append(key)
        if key in self.fail_delete:
            raise RuntimeError("synthetic cleanup failure")
        self.configured.discard(key)


def _reconcile(monkeypatch, camera, assignment, clients, *, max_changes=10):
    async def media(node):
        return clients[node.id]

    monkeypatch.setattr(reconciler.node_clients, "media", media)
    return asyncio.run(
        reconciler._distributed_reconcile(
            [camera],
            {},
            {(camera.id, "media"): assignment},
            {node_id: _node(node_id) for node_id in clients},
            max_changes=max_changes,
        )
    )


def test_dirty_generation_refreshes_existing_third_source(monkeypatch):
    """Refreshing credentials or profile must replace an already present third path."""
    camera = _camera()
    assignment = _assignment(camera, dirty=True)
    client = _MediaClient([camera.stream_key, make_role_stream_key(camera.stream_key, "main"), camera.third_stream_key])

    changed, failed, _last = _reconcile(
        monkeypatch, camera, assignment, {"media-new": client}
    )

    assert failed == 0
    assert changed == 3
    assert [key for key, _source in client.added] == [
        camera.stream_key,
        make_role_stream_key(camera.stream_key, "main"),
        camera.third_stream_key,
    ]
    assert client.added[1][1].endswith("/main")
    assert client.added[2][1].endswith("/third-new")
    assert assignment.applied_generation == assignment.generation


def test_media_cleanup_removes_all_keys_before_clearing_old_owner(monkeypatch):
    """One successful live deletion must not lose the stale third-stream cleanup."""
    camera = _camera()
    assignment = _assignment(camera, cleanup=["media-old"])
    clients = {
        key: _MediaClient([camera.stream_key, make_role_stream_key(camera.stream_key, "main"), camera.third_stream_key])
        for key in ["media-old", "media-new"]
    }

    changed, failed, _last = _reconcile(monkeypatch, camera, assignment, clients)

    assert failed == 0
    assert changed == 3
    assert set(clients["media-old"].deleted) == {
        camera.stream_key,
        make_role_stream_key(camera.stream_key, "main"),
        camera.third_stream_key,
    }
    assert assignment.cleanup_node_ids_json == []


def test_third_cleanup_failure_retains_pending_owner(monkeypatch):
    """Retry ownership cleanup until every media key was removed successfully."""
    camera = _camera()
    assignment = _assignment(camera, cleanup=["media-old"])
    clients = {
        "media-new": _MediaClient([camera.stream_key, make_role_stream_key(camera.stream_key, "main"), camera.third_stream_key]),
        "media-old": _MediaClient(
            [camera.stream_key, make_role_stream_key(camera.stream_key, "main"), camera.third_stream_key],
            fail_delete=[camera.third_stream_key],
        ),
    }

    _changed, failed, _last = _reconcile(monkeypatch, camera, assignment, clients)

    assert failed == 1
    assert assignment.cleanup_node_ids_json == ["media-old"]
    assert camera.third_stream_key in clients["media-old"].configured


def test_third_apply_failure_does_not_accept_generation_or_cleanup(monkeypatch):
    """An incomplete refresh must remain dirty and preserve the previous owner."""
    camera = _camera()
    assignment = _assignment(camera, dirty=True, cleanup=["media-old"])
    clients = {
        "media-new": _MediaClient([camera.stream_key, make_role_stream_key(camera.stream_key, "main")], fail_add=[camera.third_stream_key]),
        "media-old": _MediaClient([camera.stream_key, make_role_stream_key(camera.stream_key, "main"), camera.third_stream_key]),
    }

    _changed, failed, _last = _reconcile(monkeypatch, camera, assignment, clients)

    assert failed == 1
    assert assignment.applied_generation != assignment.generation
    assert assignment.cleanup_node_ids_json == ["media-old"]
    assert clients["media-old"].deleted == []
    assert camera.desired_state != "provisioned"


def test_third_upstream_exception_text_is_not_logged(monkeypatch, caplog):
    """Upstream errors may contain RTSP credentials and must be redacted."""
    camera = _camera()
    assignment = _assignment(camera, dirty=True)
    client = _MediaClient([camera.stream_key, make_role_stream_key(camera.stream_key, "main")])

    async def unsafe_failure(key, _source):
        if key == camera.third_stream_key:
            raise RuntimeError("rtsp://user:synthetic-password@192.168.1.20/third")

    client.add_or_replace_path = unsafe_failure
    _changed, failed, _last = _reconcile(
        monkeypatch, camera, assignment, {"media-new": client}
    )

    assert failed == 1
    assert "synthetic-password" not in caplog.text


def test_partial_refresh_at_change_budget_does_not_claim_ready(monkeypatch):
    """A one-change budget must not accept an unrefreshed third source."""
    camera = _camera()
    assignment = _assignment(camera, dirty=True, cleanup=["media-old"])
    clients = {
        key: _MediaClient([camera.stream_key, camera.third_stream_key])
        for key in ["media-old", "media-new"]
    }

    changed, failed, _last = _reconcile(
        monkeypatch, camera, assignment, clients, max_changes=1
    )

    assert failed == 0
    assert changed <= 1
    assert assignment.applied_generation != assignment.generation
    assert assignment.cleanup_node_ids_json == ["media-old"]
    assert clients["media-old"].deleted == []


def test_failed_commit_cleans_newly_allocated_third_path(monkeypatch):
    """Rollback must remove a third path whose key was absent in the old snapshot."""
    camera = _camera()
    camera.third_path = None
    camera.third_stream_key = None
    snapshot = camera_lifecycle.source_snapshot(camera)
    camera.third_path = "/third-new"
    camera.third_stream_key = "qa-gate-third"
    client = _MediaClient([camera.stream_key, make_role_stream_key(camera.stream_key, "main")])
    monkeypatch.setattr(camera_lifecycle.settings, "placement_execution_enabled", False)
    monkeypatch.setattr(camera_lifecycle.mediamtx, "add_or_replace_path", client.add_or_replace_path)
    monkeypatch.setattr(camera_lifecycle.mediamtx, "delete_path", client.delete_path)
    session = SimpleNamespace(
        commit=AsyncMock(side_effect=RuntimeError("synthetic commit failure")),
        rollback=AsyncMock(),
    )

    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        asyncio.run(camera_lifecycle.commit_source_mutation(session, camera, None, snapshot))

    assert client.configured == {camera.stream_key, make_role_stream_key(camera.stream_key, "main")}
    assert camera.third_path is None
    assert camera.third_stream_key is None
    session.rollback.assert_awaited_once()


def test_reconcile_retries_orphan_after_failed_compensating_delete(monkeypatch):
    """A rolled-back third key is still discoverable if its first cleanup fails."""
    camera = _camera()
    camera.third_path = None
    camera.third_stream_key = None
    orphan = make_role_stream_key(camera.stream_key, "third")
    present = {camera.stream_key, make_role_stream_key(camera.stream_key, "main"), orphan}
    delete = AsyncMock()
    monkeypatch.setattr(reconciler.mediamtx, "delete_path", delete)

    changed, failed = asyncio.run(reconciler.reconcile_batch([camera], present))

    assert (changed, failed) == (1, 0)
    assert orphan not in present
    delete.assert_awaited_once_with(orphan)


def test_retained_third_key_stays_in_fencing_after_path_clear():
    """Removing desired third source must retain its possible execution cleanup key."""
    camera = _camera()
    camera.third_path = None

    assert fencing._execution_keys(camera, "media", None) == [
        camera.stream_key,
        make_role_stream_key(camera.stream_key, "main"),
        camera.third_stream_key,
    ]


def test_delete_camera_cleans_retained_third_key(monkeypatch):
    """Deletion must clear a retained key after prior third-source cleanup failed."""
    camera = _camera()
    camera.third_path = None
    client = _MediaClient([camera.stream_key, camera.third_stream_key])
    monkeypatch.setattr(cameras, "authorized_camera", AsyncMock(return_value=camera))
    monkeypatch.setattr(cameras.settings, "placement_execution_enabled", False)
    monkeypatch.setattr(cameras.mediamtx, "delete_path", client.delete_path)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)),
        delete=AsyncMock(),
        commit=AsyncMock(),
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
    )

    asyncio.run(cameras.delete_camera(camera.id, session, None))

    assert client.configured == set()
    session.delete.assert_awaited_once_with(camera)


def test_delete_camera_cleans_derived_orphan_after_failed_rollback(monkeypatch):
    """A lost third key must not make a successful delete orphan its media path."""
    camera = _camera()
    camera.third_path = None
    camera.third_stream_key = None
    orphan = make_role_stream_key(camera.stream_key, "third")
    client = _MediaClient([camera.stream_key, orphan])
    monkeypatch.setattr(cameras, "authorized_camera", AsyncMock(return_value=camera))
    monkeypatch.setattr(cameras.settings, "placement_execution_enabled", False)
    monkeypatch.setattr(cameras.mediamtx, "delete_path", client.delete_path)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)),
        delete=AsyncMock(),
        commit=AsyncMock(),
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
    )

    asyncio.run(cameras.delete_camera(camera.id, session, None))

    assert client.configured == set()
    session.delete.assert_awaited_once_with(camera)


def test_same_generation_snapshot_cannot_forget_prior_execution_key(monkeypatch, tmp_path):
    """A shorter refresh still fences every path accepted in that generation."""
    module_path = Path(__file__).parents[1] / "services" / "node-agent" / "main.py"
    spec = importlib.util.spec_from_file_location("node_agent_category1_qa", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "datetime", FrozenDateTime)
    agent = module.NodeAgent(
        module.NodeAgentSettings(
            NODE_ID="media-new",
            REGION_ID="region-a",
            NODE_ROLES=["media"],
            NODE_AGENT_TOKEN="synthetic-qa-token",
            CONTROL_API_URL="http://control-api:8000",
            MEDIAMTX_API_URL="http://mediamtx:9997",
            FENCE_STATE_PATH=str(tmp_path / "fence.json"),
            FENCE_EXPIRY_GRACE_SECONDS=0,
        )
    )
    deleted = []

    async def delete(role, key):
        deleted.append((role, key))
        return True

    monkeypatch.setattr(agent, "_delete_execution_key", delete)
    item = {
        "assignment_id": "qa-assignment",
        "camera_id": "qa-camera",
        "role": "media",
        "generation": 4,
        "lease_expires_at": (FIXED_NOW - timedelta(seconds=1)).isoformat(),
        "execution_key": "qa-gate",
        "execution_keys": ["qa-gate", "qa-gate-third"],
    }
    snapshot = {
        "server_time": FIXED_NOW.isoformat(),
        "node_id": "media-new",
        "assignments": [item],
        "revocations": [],
    }

    async def scenario():
        try:
            await agent._apply_fence_snapshot(snapshot)
            item["execution_keys"] = ["qa-gate"]
            await agent._apply_fence_snapshot(snapshot)
            await agent._enforce_cached_expiry()
        finally:
            await agent.close()

    asyncio.run(scenario())

    assert set(deleted) == {("media", "qa-gate"), ("media", "qa-gate-third")}


@pytest.mark.parametrize("route", ["serial", "qr"])
@pytest.mark.parametrize("scope", [{"tenant_id": "tenant-b"}, {"site_id": "site-b"}])
def test_alternate_onboarding_denies_scope_before_network(monkeypatch, route, scope):
    """Serial and QR onboarding must reject cross-tenant/site access before IO."""
    principal = Principal(
        subject="qa-user",
        roles=frozenset({"operator"}),
        tenant_id="tenant-a",
        site_ids=frozenset({"site-a"}),
    )
    fields = {"tenant_id": "tenant-a", "site_id": "site-a", "name": "QA", **scope}
    probe = AsyncMock()
    discover = AsyncMock()
    monkeypatch.setattr(onvif_router, "probe_host", probe)
    monkeypatch.setattr(onvif_router, "discover", discover)
    if route == "serial":
        payload = OnvifSerialOnboardRequest(**fields, serial_number="QA-001")
        call = onvif_router.onboard_by_serial(payload, object(), principal)
    else:
        encoded = base64.urlsafe_b64encode(
            json.dumps({**fields, "host": "192.168.1.20"}).encode()
        ).decode().rstrip("=")
        payload = OnvifQrOnboardRequest(qr_payload=QR_PREFIX + encoded)
        call = onvif_router.onboard_by_qr(payload, object(), principal)

    with pytest.raises(HTTPException) as error:
        asyncio.run(call)

    assert error.value.status_code == 404
    probe.assert_not_awaited()
    discover.assert_not_called()


def test_serial_reassertion_rejects_device_change_before_persistence(monkeypatch):
    """A swapped device must not be onboarded after matching during discovery."""
    principal = Principal(
        subject="qa-user", roles=frozenset({"operator"}),
        tenant_id="tenant-a", site_ids=frozenset({"site-a"}),
    )
    probe = AsyncMock(return_value={"device_info": {"SerialNumber": "OTHER"}})
    monkeypatch.setattr(onvif_router, "probe_host", probe)
    request = OnvifOnboardRequest(
        tenant_id="tenant-a", site_id="site-a", name="Gate",
        host="192.168.1.20", expected_serial_number="QA-001",
    )
    session = SimpleNamespace(rollback=AsyncMock())

    with pytest.raises(HTTPException) as error:
        asyncio.run(onvif_router.onboard_device(request, session, principal))

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "SERIAL_CHANGED"
    probe.assert_awaited_once()
    session.rollback.assert_awaited_once()


def test_credential_decorated_qr_endpoint_never_reaches_onvif_network(monkeypatch):
    """A QR endpoint query or fragment must fail before opening a camera socket."""
    import app.services.onvif_client as client

    network = AsyncMock()
    monkeypatch.setattr(client, "probe_xaddr", network)
    for path in ("/onvif/device_service?token=secret", "/onvif/device_service#secret"):
        with pytest.raises(OnvifError) as error:
            asyncio.run(probe_host(
                "192.168.1.20", 80, None, None, path, "http",
                tenant_id="tenant-a", site_id="site-a",
            ))
        assert error.value.code == "DEVICE_SERVICE_INVALID"
        assert "secret" not in error.value.message
    network.assert_not_awaited()


def test_device_advertised_query_does_not_enter_public_capabilities(monkeypatch):
    """Reject query-bearing ONVIF service URLs before pinning/persistence."""
    import app.services.network_policy as policy

    resolver = lambda *_args: "192.168.1.20"
    monkeypatch.setattr(policy, "resolve_site_pinned_host", resolver)
    with pytest.raises(TargetNotAllowed):
        pin_site_http_xaddr(
            "http://192.168.1.20/onvif/media?token=synthetic-password",
            "tenant-a", "site-a",
        )


def test_qr_decoder_rejects_malformed_base64_without_internal_error():
    """Malformed credential-free QR text yields a bounded client error."""
    with pytest.raises(OnvifError) as error:
        decode_qr_payload(QR_PREFIX + "%%%")
    assert error.value.code == "INVALID_QR_PAYLOAD"


def test_refresh_missing_third_profile_disables_media_source(monkeypatch):
    """Capability refresh cannot retain an active orphan third media profile."""
    camera = _camera()
    capability = SimpleNamespace(
        camera_id=camera.id, onvif_xaddr="http://192.168.1.20/onvif/device_service",
        third_profile_token="gone", profiles_json=[{"token": "gone"}],
        device_info_json={}, services_json=[], features_json={}, probed_at=FIXED_NOW,
        main_profile_token="main", sub_profile_token="sub",
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: capability)),
        commit=AsyncMock(), refresh=AsyncMock(), rollback=AsyncMock(),
    )
    monkeypatch.setattr(onvif_router, "authorized_camera", AsyncMock(return_value=camera))
    monkeypatch.setattr(onvif_router, "probe_xaddr", AsyncMock(return_value={}))
    monkeypatch.setattr(onvif_router, "public_probe", lambda _probe: {
        "device_info": {}, "services": [], "features": {}, "profiles": [],
    })
    monkeypatch.setattr(onvif_router, "prepare_source_mutation", AsyncMock(
        return_value=(camera_lifecycle.source_snapshot(camera), None)
    ))
    commit = AsyncMock()
    monkeypatch.setattr(onvif_router, "commit_source_mutation", commit)

    asyncio.run(onvif_router.refresh_capabilities(camera.id, session, object()))

    assert camera.third_path is None
    assert camera.third_stream_key == "qa-gate-third"
    assert capability.third_profile_token is None
    commit.assert_awaited_once()
    session.commit.assert_not_awaited()
