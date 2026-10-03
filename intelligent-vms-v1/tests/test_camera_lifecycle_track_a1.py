import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.core.auth import Principal
from app.models.entities import CameraCapabilityEntity, CameraEntity, CameraGroupEntity
from app.models.schemas import (
    CameraCredentialUpdate,
    CameraReplacement,
    CameraUpdate,
)
from app.routers import cameras
from app.services import camera_lifecycle


def principal(*, tenant_id="tenant-a", sites=("site-a",)):
    return Principal(
        subject="operator-1",
        roles=frozenset({"operator"}),
        tenant_id=tenant_id,
        site_ids=frozenset(sites),
    )


def camera():
    return CameraEntity(
        id="camera-1",
        tenant_id="tenant-a",
        site_id="site-a",
        name="Gate",
        location_description=None,
        group_id=None,
        host="192.168.1.20",
        rtsp_port=554,
        main_path="/main",
        sub_path="/sub",
        username_enc="enc-old-user",
        password_enc="enc-old-pass",
        stream_key="site-a-gate",
        media_node_id="media-local-01",
        enabled=True,
        desired_state="provisioned",
    )


def group(*, group_id="group-a", tenant_id="tenant-a", site_id="site-a"):
    return CameraGroupEntity(
        id=group_id,
        tenant_id=tenant_id,
        site_id=site_id,
        name="Entrances",
        description="Entry cameras",
    )


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return list(self.value or [])


class Session:
    def __init__(self, cam=None, groups=None, execute_values=None, commit_error=None):
        self.cam = cam
        self.groups = groups or {}
        self.execute_values = list(execute_values or [])
        self.deleted = []
        self.committed = False
        self.rolled_back = False
        self.commit_error = commit_error

    async def get(self, model, key):
        if model is CameraEntity:
            return self.cam if self.cam and self.cam.id == key else None
        if model is CameraGroupEntity:
            return self.groups.get(key)
        return None

    async def execute(self, query):
        del query
        if self.execute_values:
            return ScalarResult(self.execute_values.pop(0))
        return ScalarResult(None)

    async def commit(self):
        if self.commit_error:
            raise self.commit_error
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def refresh(self, value):
        del value

    async def delete(self, value):
        self.deleted.append(value)

    def add(self, value):
        del value


def test_metadata_update_preserves_camera_and_stream_identity(monkeypatch):
    cam = camera()
    grp = group()
    session = Session(cam=cam, groups={grp.id: grp})
    original_id = cam.id
    original_stream = cam.stream_key
    monkeypatch.setattr(cameras, "to_read", lambda entity, node=None: entity)

    result = asyncio.run(
        cameras.update_camera(
            cam.id,
            CameraUpdate(
                name="Main Gate",
                location_description="Building A entrance",
                group_id=grp.id,
            ),
            session,
            principal(),
        )
    )

    assert result.id == original_id
    assert result.stream_key == original_stream
    assert result.name == "Main Gate"
    assert result.location_description == "Building A entrance"
    assert result.group_id == grp.id
    assert session.committed is True


def test_cross_site_group_assignment_is_rejected():
    cam = camera()
    other = group(group_id="group-b", site_id="site-b")
    session = Session(cam=cam, groups={other.id: other})

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            cameras.update_camera(
                cam.id,
                CameraUpdate(group_id=other.id),
                session,
                principal(),
            )
        )

    assert error.value.status_code == 422
    assert error.value.detail["code"] == "INVALID_CAMERA_GROUP"
    assert cam.group_id is None


def test_credential_update_preserves_identity_and_never_returns_credentials(monkeypatch):
    cam = camera()
    session = Session(cam=cam)
    original_stream = cam.stream_key
    old_password = cam.password_enc
    monkeypatch.setattr(
        cameras,
        "prepare_source_mutation",
        AsyncMock(return_value=({"host": cam.host}, None)),
    )
    commit = AsyncMock()
    monkeypatch.setattr(cameras, "commit_source_mutation", commit)
    monkeypatch.setattr(cameras, "encrypt_secret", lambda value: f"enc:{value}" if value else None)
    monkeypatch.setattr(cameras, "to_read", lambda entity, node=None: entity)

    result = asyncio.run(
        cameras.update_camera_credentials(
            cam.id,
            CameraCredentialUpdate(username="new-user"),
            session,
            principal(),
        )
    )

    assert result.id == "camera-1"
    assert result.stream_key == original_stream
    assert cam.username_enc == "enc:new-user"
    assert cam.password_enc == old_password
    commit.assert_awaited_once()
    assert "new-user" not in str(cameras.to_read(cam).__dict__.get("password_enc", ""))


def test_replacement_preserves_logical_identity_and_invalidates_capability(monkeypatch):
    cam = camera()
    capability = CameraCapabilityEntity(
        id="cap-1",
        camera_id=cam.id,
        onvif_xaddr="http://192.168.1.20/onvif/device_service",
        device_info_json={"FirmwareVersion": "old"},
        services_json=[],
        features_json={},
        profiles_json=[],
    )
    session = Session(cam=cam, execute_values=[capability])
    original_id = cam.id
    original_stream = cam.stream_key
    monkeypatch.setattr(
        cameras,
        "validate_site_camera_rtsp_target",
        lambda host, port, path, tenant_id, site_id: ("192.168.1.30", port, path),
    )
    monkeypatch.setattr(
        cameras,
        "prepare_source_mutation",
        AsyncMock(return_value=({"host": "192.168.1.20"}, None)),
    )
    commit = AsyncMock()
    monkeypatch.setattr(cameras, "commit_source_mutation", commit)
    monkeypatch.setattr(cameras, "encrypt_secret", lambda value: f"enc:{value}" if value else None)
    monkeypatch.setattr(cameras, "to_read", lambda entity, node=None: entity)

    result = asyncio.run(
        cameras.replace_camera(
            cam.id,
            CameraReplacement(
                host="replacement.local",
                rtsp_port=8554,
                main_path="/new-main",
                sub_path="/new-sub",
                username="replacement-user",
                password="synthetic-secret-marker",
            ),
            session,
            principal(),
        )
    )

    assert result.id == original_id
    assert result.stream_key == original_stream
    assert result.host == "192.168.1.30"
    assert result.rtsp_port == 8554
    assert capability in session.deleted
    commit.assert_awaited_once()
    assert "synthetic-secret-marker" not in str(result.name)


def test_replacement_target_denial_does_not_mutate_camera(monkeypatch):
    cam = camera()
    session = Session(cam=cam)
    monkeypatch.setattr(
        cameras,
        "validate_site_camera_rtsp_target",
        lambda *_args: (_ for _ in ()).throw(
            cameras.TargetNotAllowed("synthetic-secret-marker")
        ),
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            cameras.replace_camera(
                cam.id,
                CameraReplacement(
                    host="blocked.example",
                    main_path="/main",
                ),
                session,
                principal(),
            )
        )

    assert error.value.status_code == 400
    assert "synthetic-secret-marker" not in str(error.value.detail)
    assert cam.host == "192.168.1.20"


def test_distributed_source_refresh_keeps_generation_and_owner(monkeypatch):
    cam = camera()
    assignment = SimpleNamespace(
        camera_id=cam.id,
        role="media",
        node_id="node-a",
        generation=7,
        applied_generation=7,
    )
    session = Session(cam=cam, execute_values=[[assignment]])
    monkeypatch.setattr(
        camera_lifecycle,
        "require_placement_execution_lock",
        AsyncMock(),
    )

    asyncio.run(camera_lifecycle.mark_distributed_source_dirty(session, cam))

    assert assignment.node_id == "node-a"
    assert assignment.generation == 7
    assert assignment.applied_generation is None
    assert cam.desired_state == "pending-source-refresh"


def test_commit_failure_restores_previous_local_source(monkeypatch):
    cam = camera()
    snapshot = camera_lifecycle.source_snapshot(cam)
    session = Session(cam=cam, commit_error=RuntimeError("db unavailable"))
    applied_hosts = []

    async def capture_apply(entity, policy):
        del policy
        applied_hosts.append(entity.host)

    monkeypatch.setattr(camera_lifecycle.settings, "placement_execution_enabled", False)
    monkeypatch.setattr(camera_lifecycle, "apply_single_node_source", capture_apply)

    cam.host = "192.168.1.30"
    with pytest.raises(RuntimeError):
        asyncio.run(
            camera_lifecycle.commit_source_mutation(
                session,
                cam,
                None,
                snapshot,
            )
        )

    assert applied_hosts == ["192.168.1.30", "192.168.1.20"]
    assert cam.host == "192.168.1.20"
    assert session.rolled_back is True


def test_credential_update_returns_conflict_when_placement_is_busy(monkeypatch):
    cam = camera()
    session = Session(cam=cam)
    monkeypatch.setattr(
        cameras,
        "prepare_source_mutation",
        AsyncMock(return_value=({"host": cam.host}, None)),
    )
    monkeypatch.setattr(
        cameras,
        "commit_source_mutation",
        AsyncMock(side_effect=cameras.PlacementExecutionBusy("busy")),
    )
    monkeypatch.setattr(cameras, "encrypt_secret", lambda value: f"enc:{value}")

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            cameras.update_camera_credentials(
                cam.id,
                CameraCredentialUpdate(password="synthetic-secret-marker"),
                session,
                principal(),
            )
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "PLACEMENT_BUSY"
    assert "synthetic-secret-marker" not in str(error.value.detail)
