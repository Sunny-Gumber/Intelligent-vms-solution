import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, require_roles, require_scope
from app.core.config import settings
from app.core.security import encrypt_secret
from app.db.session import get_session
from app.models.entities import (
    CameraCapabilityEntity,
    CameraEntity,
    CameraGroupEntity,
    RecordingPolicyEntity,
    ManualRecordingSessionEntity,
)
from app.models.placement import InfrastructureNodeEntity, PlacementAssignmentEntity
from app.models.schemas import (
    CameraCreate,
    CameraCredentialUpdate,
    CameraGroupCreate,
    CameraGroupRead,
    CameraGroupUpdate,
    CameraHealth,
    CameraRead,
    CameraReplacement,
    CameraUpdate,
)
from app.services.camera_lifecycle import (
    available_live_roles,
    commit_source_mutation,
    main_live_source,
    main_live_stream_key,
    prepare_source_mutation,
)
from app.services.coordination import PlacementExecutionBusy, require_placement_execution_lock
from app.services.mediamtx import mediamtx
from app.services.network_policy import (
    TargetNotAllowed,
    validate_site_camera_rtsp_target,
)
from app.services.node_media import NodeEndpointError, assigned_node, node_clients, public_media_bases
from app.services.rtsp import build_rtsp_uri, source_trust_options
from app.services.stream_keys import make_role_stream_key, make_stream_key

router = APIRouter(prefix="/api/v1/cameras", tags=["cameras"])
group_router = APIRouter(prefix="/api/v1/camera-groups", tags=["camera-groups"])
log = logging.getLogger(__name__)


def to_read(entity: CameraEntity, node: InfrastructureNodeEntity | None = None) -> CameraRead:
    """Convert a camera ORM row into the public camera response model.

    Args:
        entity: Persisted camera row to serialize.
        node: Optional assigned infrastructure node used to derive public media URLs.

    Returns:
        CameraRead with public live-view URLs when valid endpoints are available.
    """
    webrtc_url: str | None = None
    hls_url: str | None = None
    third_webrtc_url: str | None = None
    third_hls_url: str | None = None
    if node is not None:
        if entity.desired_state == "provisioned":
            try:
                webrtc_base, hls_base = public_media_bases(node)
                webrtc_url = f"{webrtc_base}/{entity.stream_key}"
                hls_url = f"{hls_base}/{entity.stream_key}"
                if entity.third_path and entity.third_stream_key:
                    third_webrtc_url = f"{webrtc_base}/{entity.third_stream_key}"
                    third_hls_url = f"{hls_base}/{entity.third_stream_key}"
            except NodeEndpointError:
                log.warning("camera_public_endpoint_invalid camera_id=%s node_id=%s", entity.id, node.id)
    elif not settings.placement_execution_enabled:
        webrtc_url = f"{settings.mediamtx_webrtc_public_base.rstrip('/')}/{entity.stream_key}"
        hls_url = f"{settings.mediamtx_hls_public_base.rstrip('/')}/{entity.stream_key}"
        if entity.third_path and entity.third_stream_key:
            third_webrtc_url = (
                f"{settings.mediamtx_webrtc_public_base.rstrip('/')}/{entity.third_stream_key}"
            )
            third_hls_url = (
                f"{settings.mediamtx_hls_public_base.rstrip('/')}/{entity.third_stream_key}"
            )
    return CameraRead(
        id=entity.id,
        tenant_id=entity.tenant_id,
        site_id=entity.site_id,
        name=entity.name,
        location_description=entity.location_description,
        group_id=entity.group_id,
        host=entity.host,
        rtsp_port=entity.rtsp_port,
        source_protocol=entity.source_protocol or "rtsp",
        source_fingerprint=entity.source_fingerprint,
        stream_key=entity.stream_key,
        third_stream_key=entity.third_stream_key,
        available_live_roles=available_live_roles(entity),
        media_node_id=entity.media_node_id,
        enabled=entity.enabled,
        desired_state=entity.desired_state,
        created_at=entity.created_at,
        webrtc_url=webrtc_url,
        hls_url=hls_url,
        third_webrtc_url=third_webrtc_url,
        third_hls_url=third_hls_url,
    )


async def _node_map(session: AsyncSession, rows: list[CameraEntity]) -> dict[str, InfrastructureNodeEntity]:
    ids = sorted({row.media_node_id for row in rows if row.media_node_id})
    if not ids:
        return {}
    nodes = (
        await session.execute(
            select(InfrastructureNodeEntity).where(InfrastructureNodeEntity.id.in_(ids))
        )
    ).scalars().all()
    return {node.id: node for node in nodes}


async def authorized_camera(
    session: AsyncSession, camera_id: str, principal: Principal
) -> CameraEntity:
    """Load a camera and enforce tenant/site authorization.

    Args:
        session: Database session used to load the camera.
        camera_id: Camera identifier requested by the caller.
        principal: Authenticated identity whose scope must cover the camera.

    Returns:
        Authorized camera ORM row.

    Raises:
        HTTPException: HTTP 404 when the camera is absent or outside scope.
    """
    entity = await session.get(CameraEntity, camera_id)
    if not entity:
        raise HTTPException(404, "Camera not found")
    require_scope(principal, entity.tenant_id, entity.site_id)
    return entity


@router.post("", response_model=CameraRead, status_code=status.HTTP_201_CREATED)
async def create_camera(
    payload: CameraCreate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Create a camera and provision its live path when running single-node.

    Args:
        payload: Validated camera connection and scope data.
        session: Database session for persistence.
        principal: Authorized administrator or operator.

    Returns:
        Public camera representation after successful persistence/provisioning.

    Raises:
        HTTPException: If scope authorization or media provisioning fails.
    """
    require_scope(principal, payload.tenant_id, payload.site_id)
    try:
        pinned_host, rtsp_port, main_path = validate_site_camera_rtsp_target(
            payload.host,
            payload.rtsp_port,
            payload.main_path,
            payload.tenant_id,
            payload.site_id,
        )
        sub_path = None
        if payload.sub_path:
            _host, _port, sub_path = validate_site_camera_rtsp_target(
                pinned_host,
                rtsp_port,
                payload.sub_path,
                payload.tenant_id,
                payload.site_id,
            )
        third_path = None
        if payload.third_path:
            _host, _port, third_path = validate_site_camera_rtsp_target(
                pinned_host,
                rtsp_port,
                payload.third_path,
                payload.tenant_id,
                payload.site_id,
            )
    except (TargetNotAllowed, OSError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "TARGET_NOT_ALLOWED", "message": "Camera target is not allowed"},
        ) from exc

    stream_key = make_stream_key(payload.site_id, payload.name)
    entity = CameraEntity(
        tenant_id=payload.tenant_id,
        site_id=payload.site_id,
        name=payload.name,
        host=pinned_host,
        rtsp_port=rtsp_port,
        source_protocol=payload.source_protocol,
        source_fingerprint=payload.source_fingerprint,
        main_path=main_path,
        sub_path=sub_path,
        third_path=third_path,
        third_stream_key=(
            make_role_stream_key(stream_key, "third") if third_path else None
        ),
        username_enc=encrypt_secret(payload.username),
        password_enc=encrypt_secret(payload.password),
        stream_key=stream_key,
        desired_state="pending-placement" if settings.placement_execution_enabled else "provisioned",
    )
    session.add(entity)
    await session.flush()

    provisioned_keys: list[str] = []
    try:
        if not settings.placement_execution_enabled:
            source = build_rtsp_uri(
                entity.host,
                entity.rtsp_port,
                entity.sub_path or entity.main_path,
                payload.username,
                payload.password,
                source_protocol=getattr(entity, "source_protocol", None) or "rtsp",
            )
            provisioned_keys.append(entity.stream_key)
            await mediamtx.add_or_replace_path(entity.stream_key, source, **source_trust_options(entity))
            if entity.sub_path:
                main_key = main_live_stream_key(entity)
                provisioned_keys.append(main_key)
                await mediamtx.add_or_replace_path(main_key, main_live_source(entity), **source_trust_options(entity))
            if entity.third_path and entity.third_stream_key:
                third_source = build_rtsp_uri(
                    entity.host,
                    entity.rtsp_port,
                    entity.third_path,
                    payload.username,
                    payload.password,
                    source_protocol=getattr(entity, "source_protocol", None) or "rtsp",
                )
                provisioned_keys.append(entity.third_stream_key)
                await mediamtx.add_or_replace_path(
                    entity.third_stream_key,
                    third_source,
                    **source_trust_options(entity),
                )
        await session.commit()
    except Exception as exc:
        for stream_key in reversed(provisioned_keys):
            try:
                await mediamtx.delete_path(stream_key)
            except Exception:
                log.warning(
                    "camera_create_cleanup_failed stream_key=%s",
                    stream_key,
                )
        await session.rollback()
        log.warning("camera_provisioning_failed")
        raise HTTPException(
            status_code=502,
            detail={"code": "MEDIA_PROVISION_FAILED", "message": "Camera could not be provisioned"},
        ) from exc

    await session.refresh(entity)
    return to_read(entity)


@router.get("", response_model=list[CameraRead])
async def list_cameras(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """List cameras visible to the authenticated tenant/site scope.

    Args:
        session: Database session used to query cameras and node metadata.
        principal: Authenticated administrator, operator or viewer.

    Returns:
        CameraRead list restricted to the principal scope.
    """
    query = select(CameraEntity).order_by(CameraEntity.created_at.desc())
    if principal.tenant_id != "*":
        query = query.where(CameraEntity.tenant_id == principal.tenant_id)
    if "*" not in principal.site_ids:
        query = query.where(CameraEntity.site_id.in_(principal.site_ids))
    rows = list((await session.execute(query)).scalars().all())
    nodes = await _node_map(session, rows)
    return [to_read(row, nodes.get(row.media_node_id)) for row in rows]


@router.get("/{camera_id}", response_model=CameraRead)
async def get_camera(
    camera_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """Return one authorized camera and its public media endpoints.

    Args:
        camera_id: Camera identifier to retrieve.
        session: Database session used to load camera/node state.
        principal: Authenticated identity whose scope is enforced.

    Returns:
        CameraRead for the requested camera.

    Raises:
        HTTPException: HTTP 404 when the camera is absent or outside scope.
    """
    camera = await authorized_camera(session, camera_id, principal)
    node = await session.get(InfrastructureNodeEntity, camera.media_node_id)
    return to_read(camera, node)


async def _authorized_group(
    session: AsyncSession,
    group_id: str,
    principal: Principal,
) -> CameraGroupEntity:
    """Load one camera group and enforce tenant/site authorization.

    Args:
        session: Database session used to load the group.
        group_id: Camera-group identifier.
        principal: Authenticated caller whose scope must cover the group.

    Returns:
        Authorized camera-group entity.

    Raises:
        HTTPException: HTTP 404 when the group is absent or outside caller scope.
    """
    group = await session.get(CameraGroupEntity, group_id)
    if group is None:
        raise HTTPException(404, "Camera group not found")
    require_scope(principal, group.tenant_id, group.site_id)
    return group


async def _group_for_camera(
    session: AsyncSession,
    camera: CameraEntity,
    group_id: str,
) -> CameraGroupEntity:
    """Load a group and require exact tenant/site match with a camera.

    Args:
        session: Database session used for lookup.
        camera: Camera being assigned.
        group_id: Requested group identifier.

    Returns:
        Matching camera-group entity.

    Raises:
        HTTPException: HTTP 422 when the group is missing or belongs elsewhere.
    """
    group = await session.get(CameraGroupEntity, group_id)
    if (
        group is None
        or group.tenant_id != camera.tenant_id
        or group.site_id != camera.site_id
    ):
        raise HTTPException(
            422,
            {
                "code": "INVALID_CAMERA_GROUP",
                "message": "Camera group must belong to the same tenant and site",
            },
        )
    return group


@router.patch("/{camera_id}", response_model=CameraRead)
async def update_camera(
    camera_id: str,
    payload: CameraUpdate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Update logical camera name, location and/or group assignment.

    Args:
        camera_id: Camera identifier to update.
        payload: Validated metadata patch.
        session: Database session used for persistence.
        principal: Authorized administrator or operator.

    Returns:
        Updated public camera representation with stable camera/stream identity.

    Raises:
        HTTPException: If authorization or group assignment validation fails.
    """
    camera = await authorized_camera(session, camera_id, principal)
    if "group_id" in payload.model_fields_set:
        if payload.group_id is not None:
            await _group_for_camera(session, camera, payload.group_id)
        camera.group_id = payload.group_id
    if "name" in payload.model_fields_set and payload.name is not None:
        camera.name = payload.name
    if "location_description" in payload.model_fields_set:
        camera.location_description = payload.location_description
    await session.commit()
    await session.refresh(camera)
    node = await session.get(InfrastructureNodeEntity, camera.media_node_id)
    return to_read(camera, node)


@router.patch("/{camera_id}/credentials", response_model=CameraRead)
async def update_camera_credentials(
    camera_id: str,
    payload: CameraCredentialUpdate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Rotate or clear camera credentials without changing logical identity.

    Args:
        camera_id: Camera whose source credentials are changing.
        payload: Explicit credential fields to rotate/clear.
        session: Database session used for source persistence and placement state.
        principal: Authorized administrator or operator.

    Returns:
        Updated public camera representation.

    Raises:
        HTTPException: If authorization or safe media-source refresh fails.
    """
    camera = await authorized_camera(session, camera_id, principal)
    snapshot, policy = await prepare_source_mutation(session, camera)
    if "username" in payload.model_fields_set:
        camera.username_enc = encrypt_secret(payload.username)
    if "password" in payload.model_fields_set:
        camera.password_enc = encrypt_secret(payload.password)
    try:
        await commit_source_mutation(session, camera, policy, snapshot)
    except PlacementExecutionBusy as exc:
        raise HTTPException(
            409,
            {
                "code": "PLACEMENT_BUSY",
                "message": "Placement ownership is changing; retry credential update",
            },
        ) from exc
    except Exception as exc:
        log.warning("camera_credential_update_failed camera_id=%s", camera.id)
        raise HTTPException(
            502,
            {
                "code": "CAMERA_SOURCE_UPDATE_FAILED",
                "message": "Camera credentials could not be safely applied",
            },
        ) from exc
    await session.refresh(camera)
    node = await session.get(InfrastructureNodeEntity, camera.media_node_id)
    return to_read(camera, node)


@router.post("/{camera_id}/replace", response_model=CameraRead)
async def replace_camera(
    camera_id: str,
    payload: CameraReplacement,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Replace the physical source while preserving logical camera identity.

    Args:
        camera_id: Logical camera identifier to preserve.
        payload: New validated physical source fields and optional credentials.
        session: Database session used for continuity-preserving update.
        principal: Authorized administrator or operator.

    Returns:
        Updated public camera with unchanged camera ID and stream key.

    Raises:
        HTTPException: If target validation, authorization or safe source refresh fails.
    """
    camera = await authorized_camera(session, camera_id, principal)
    try:
        pinned_host, rtsp_port, main_path = validate_site_camera_rtsp_target(
            payload.host,
            payload.rtsp_port,
            payload.main_path,
            camera.tenant_id,
            camera.site_id,
        )
        sub_path = None
        if payload.sub_path:
            _host, _port, sub_path = validate_site_camera_rtsp_target(
                pinned_host,
                rtsp_port,
                payload.sub_path,
                camera.tenant_id,
                camera.site_id,
            )
        third_path = None
        if payload.third_path:
            _host, _port, third_path = validate_site_camera_rtsp_target(
                pinned_host,
                rtsp_port,
                payload.third_path,
                camera.tenant_id,
                camera.site_id,
            )
    except (TargetNotAllowed, OSError) as exc:
        raise HTTPException(
            400,
            {
                "code": "TARGET_NOT_ALLOWED",
                "message": "Replacement camera target is not allowed",
            },
        ) from exc

    snapshot, policy = await prepare_source_mutation(session, camera)
    camera.host = pinned_host
    camera.rtsp_port = rtsp_port
    # Omitted protocol preserves a secure source; downgrade requires explicit RTSP.
    if "source_protocol" in payload.model_fields_set:
        camera.source_protocol = payload.source_protocol
    camera.source_fingerprint = payload.source_fingerprint
    camera.main_path = main_path
    camera.sub_path = sub_path
    camera.third_path = third_path
    if third_path and not camera.third_stream_key:
        camera.third_stream_key = make_role_stream_key(camera.stream_key, "third")
    if "username" in payload.model_fields_set:
        camera.username_enc = encrypt_secret(payload.username)
    if "password" in payload.model_fields_set:
        camera.password_enc = encrypt_secret(payload.password)

    capability = (
        await session.execute(
            select(CameraCapabilityEntity).where(
                CameraCapabilityEntity.camera_id == camera.id
            )
        )
    ).scalar_one_or_none()
    if capability is not None:
        await session.delete(capability)

    try:
        await commit_source_mutation(session, camera, policy, snapshot)
    except PlacementExecutionBusy as exc:
        raise HTTPException(
            409,
            {
                "code": "PLACEMENT_BUSY",
                "message": "Placement ownership is changing; retry camera replacement",
            },
        ) from exc
    except Exception as exc:
        log.warning("camera_replacement_failed camera_id=%s", camera.id)
        raise HTTPException(
            502,
            {
                "code": "CAMERA_REPLACEMENT_FAILED",
                "message": "Replacement camera could not be safely applied",
            },
        ) from exc
    await session.refresh(camera)
    node = await session.get(InfrastructureNodeEntity, camera.media_node_id)
    return to_read(camera, node)


@group_router.post("", response_model=CameraGroupRead, status_code=status.HTTP_201_CREATED)
async def create_camera_group(
    payload: CameraGroupCreate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Create one tenant/site-scoped logical camera group.

    Args:
        payload: Validated group scope and metadata.
        session: Database session used for persistence.
        principal: Authorized administrator or operator.

    Returns:
        Created camera-group representation.

    Raises:
        HTTPException: If scope is unauthorized or the group name conflicts.
    """
    require_scope(principal, payload.tenant_id, payload.site_id)
    group = CameraGroupEntity(
        tenant_id=payload.tenant_id,
        site_id=payload.site_id,
        name=payload.name,
        description=payload.description,
    )
    session.add(group)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            409,
            {
                "code": "CAMERA_GROUP_EXISTS",
                "message": "Camera group name already exists in this site",
            },
        ) from exc
    await session.refresh(group)
    return CameraGroupRead.model_validate(group, from_attributes=True)


@group_router.get("", response_model=list[CameraGroupRead])
async def list_camera_groups(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """List camera groups visible to caller tenant/site scope.

    Args:
        session: Database session used for scoped query.
        principal: Authenticated administrator, operator or viewer.

    Returns:
        Authorized camera-group list ordered by name.
    """
    query = select(CameraGroupEntity).order_by(CameraGroupEntity.name)
    if principal.tenant_id != "*":
        query = query.where(CameraGroupEntity.tenant_id == principal.tenant_id)
    if "*" not in principal.site_ids:
        query = query.where(CameraGroupEntity.site_id.in_(principal.site_ids))
    rows = list((await session.execute(query)).scalars().all())
    return [CameraGroupRead.model_validate(row, from_attributes=True) for row in rows]


@group_router.get("/{group_id}", response_model=CameraGroupRead)
async def get_camera_group(
    group_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """Return one authorized camera group.

    Args:
        group_id: Group identifier to retrieve.
        session: Database session used for lookup.
        principal: Authenticated administrator, operator or viewer.

    Returns:
        Authorized camera-group representation.

    Raises:
        HTTPException: If the group is absent or outside caller scope.
    """
    group = await _authorized_group(session, group_id, principal)
    return CameraGroupRead.model_validate(group, from_attributes=True)


@group_router.patch("/{group_id}", response_model=CameraGroupRead)
async def update_camera_group(
    group_id: str,
    payload: CameraGroupUpdate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Update camera-group name/description without changing its scope.

    Args:
        group_id: Group identifier to modify.
        payload: Validated metadata patch.
        session: Database session used for persistence.
        principal: Authorized administrator or operator.

    Returns:
        Updated camera-group representation.

    Raises:
        HTTPException: If authorization fails or the new name conflicts.
    """
    group = await _authorized_group(session, group_id, principal)
    if "name" in payload.model_fields_set and payload.name is not None:
        group.name = payload.name
    if "description" in payload.model_fields_set:
        group.description = payload.description
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            409,
            {
                "code": "CAMERA_GROUP_EXISTS",
                "message": "Camera group name already exists in this site",
            },
        ) from exc
    await session.refresh(group)
    return CameraGroupRead.model_validate(group, from_attributes=True)


@group_router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera_group(
    group_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Delete a group and unassign its cameras without deleting cameras.

    Args:
        group_id: Group identifier to delete.
        session: Database session used for camera unassignment and deletion.
        principal: Authorized administrator or operator.

    Returns:
        No response body on successful HTTP 204 deletion.

    Raises:
        HTTPException: If the group is absent or outside caller scope.
    """
    group = await _authorized_group(session, group_id, principal)
    await session.execute(
        update(CameraEntity)
        .where(
            CameraEntity.group_id == group.id,
            CameraEntity.tenant_id == group.tenant_id,
            CameraEntity.site_id == group.site_id,
        )
        .values(group_id=None)
    )
    await session.delete(group)
    await session.commit()


@router.get("/{camera_id}/health", response_model=CameraHealth)
async def camera_health(
    camera_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """Return current media-path health for an authorized camera.

    Args:
        camera_id: Camera identifier to inspect.
        session: Database session used for camera/node ownership lookup.
        principal: Authenticated identity whose scope is enforced.

    Returns:
        CameraHealth derived from the assigned or local MediaMTX path state.

    Raises:
        HTTPException: If authorization fails or media-node health is unavailable.
    """
    entity = await authorized_camera(session, camera_id, principal)
    try:
        if settings.placement_execution_enabled:
            _assignment, node = await assigned_node(session, entity.id, "media")
            client = await node_clients.media(node)
            data = await client.list_paths()
        else:
            data = await mediamtx.list_paths()
    except Exception as exc:
        log.warning("camera_health_media_unavailable camera_id=%s", entity.id)
        raise HTTPException(
            502,
            {"code": "MEDIA_HEALTH_UNAVAILABLE", "message": "Media node health is unavailable"},
        ) from exc

    items = data.get("items", []) if isinstance(data, dict) else []
    match = next((item for item in items if item.get("name") == entity.stream_key), None)
    tracks = [str(track) for track in list((match or {}).get("tracks") or [])[:64]]
    safe_detail = {
        "ready": bool(match and match.get("ready")),
        "tracks": tracks,
    }
    if isinstance((match or {}).get("sourceReady"), bool):
        safe_detail["sourceReady"] = match["sourceReady"]
    return CameraHealth(
        camera_id=entity.id,
        stream_key=entity.stream_key,
        path_present=match is not None,
        ready=safe_detail["ready"],
        tracks=tracks,
        detail=safe_detail if match is not None else {},
    )


async def _delete_distributed_paths(session: AsyncSession, camera: CameraEntity) -> None:
    assignments = (
        await session.execute(
            select(PlacementAssignmentEntity).where(
                PlacementAssignmentEntity.camera_id == camera.id,
                PlacementAssignmentEntity.role.in_(["media", "recording"]),
            )
        )
    ).scalars().all()
    policy = (
        await session.execute(
            select(RecordingPolicyEntity).where(RecordingPolicyEntity.camera_id == camera.id)
        )
    ).scalar_one_or_none()

    node_ids = {
        node_id
        for assignment in assignments
        for node_id in ([assignment.node_id] + list(assignment.cleanup_node_ids_json or []))
        if node_id
    }
    nodes = {}
    if node_ids:
        rows = (
            await session.execute(
                select(InfrastructureNodeEntity).where(InfrastructureNodeEntity.id.in_(node_ids))
            )
        ).scalars().all()
        nodes = {node.id: node for node in rows}

    operations: set[tuple[str, str]] = set()
    for assignment in assignments:
        if assignment.role == "media":
            stream_key = camera.stream_key
        elif assignment.role == "recording" and policy is not None:
            stream_key = policy.record_stream_key
        else:
            continue
        operations.add((assignment.node_id, stream_key))
        for cleanup_node_id in assignment.cleanup_node_ids_json or []:
            operations.add((cleanup_node_id, stream_key))
        if assignment.role == "media":
            live_role_keys = [
                make_role_stream_key(camera.stream_key, "main"),
                camera.third_stream_key or make_role_stream_key(camera.stream_key, "third"),
            ]
            for live_role_key in live_role_keys:
                operations.add((assignment.node_id, live_role_key))
                for cleanup_node_id in assignment.cleanup_node_ids_json or []:
                    operations.add((cleanup_node_id, live_role_key))

    for node_id, stream_key in sorted(operations):
        node = nodes.get(node_id)
        if node is None:
            raise HTTPException(
                503,
                f"Cannot safely delete camera while assigned node {node_id} is unavailable",
            )
        try:
            client = await node_clients.media(node)
            await client.delete_path(stream_key)
        except Exception as exc:
            raise HTTPException(
                503,
                f"Cannot safely delete camera path from node {node_id}",
            ) from exc


@router.delete("/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera(
    camera_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Delete an authorized camera after safely removing active media paths.

    Args:
        camera_id: Camera identifier to remove.
        session: Database session for ownership checks and deletion.
        principal: Authorized administrator or operator.

    Returns:
        No response body on successful HTTP 204 deletion.

    Raises:
        HTTPException: If authorization, placement coordination or safe path
            cleanup prevents deletion.
    """
    entity = await authorized_camera(session, camera_id, principal)
    if settings.placement_execution_enabled:
        try:
            await require_placement_execution_lock(session)
        except PlacementExecutionBusy as exc:
            raise HTTPException(
                409,
                "Placement ownership is changing; retry camera deletion",
            ) from exc
        await _delete_distributed_paths(session, entity)
    else:
        policy = (
            await session.execute(
                select(RecordingPolicyEntity).where(RecordingPolicyEntity.camera_id == entity.id)
            )
        ).scalar_one_or_none()
        try:
            if policy is not None and policy.enabled and policy.mode == "continuous":
                await mediamtx.delete_path(policy.record_stream_key)
            await mediamtx.delete_path(
                entity.third_stream_key or make_role_stream_key(entity.stream_key, "third")
            )
            await mediamtx.delete_path(make_role_stream_key(entity.stream_key, "main"))
            await mediamtx.delete_path(entity.stream_key)
        except Exception as exc:
            log.warning("camera_delete_cleanup_failed camera_id=%s", entity.id)
            raise HTTPException(
                503,
                {"code": "CAMERA_CLEANUP_FAILED", "message": "Camera paths could not be safely removed"},
            ) from exc
    now = datetime.now(timezone.utc)
    await session.execute(
        update(ManualRecordingSessionEntity)
        .where(
            ManualRecordingSessionEntity.camera_id == entity.id,
            ManualRecordingSessionEntity.state == "ACTIVE",
        )
        .values(state="STOPPED", stopped_at=case((ManualRecordingSessionEntity.max_stop_at < now, ManualRecordingSessionEntity.max_stop_at), else_=now))
    )
    await session.delete(entity)
    await session.commit()
