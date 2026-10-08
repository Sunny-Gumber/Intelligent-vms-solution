from datetime import timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, require_roles, require_scope
from app.db.session import get_session
from app.models.entities import AIModelEntity, CameraAIPolicyEntity, CameraEntity
from app.models.schemas import AIIngestRead, AIModelCreate, AIModelRead, AIPolicyRead, AIPolicyUpdate, AIResultIn, AIStatusRead
from app.routers.cameras import authorized_camera
from app.services.ai import (
    publish_ai_events,
    redact_provider_config,
    validate_artifact_ref,
    validate_provider_config,
    validate_sha256,
)

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])


def model_read(row: AIModelEntity) -> AIModelRead:
    """Convert a persisted AI model row into its public response model.

    Args:
        row: AI model ORM row to serialize.

    Returns:
        AIModelRead containing public model metadata.
    """
    return AIModelRead(
        id=row.id, tenant_id=row.tenant_id, name=row.name, version=row.version,
        provider_type=row.provider_type, artifact_ref=row.artifact_ref, sha256=row.sha256,
        labels=list(row.labels_json or []), input_width=row.input_width, input_height=row.input_height,
        enabled=row.enabled, created_at=row.created_at, updated_at=row.updated_at
    )


def policy_read(row: CameraAIPolicyEntity) -> AIPolicyRead:
    """Convert a persisted camera AI policy into its public response model.

    Args:
        row: Camera AI policy ORM row to serialize.

    Returns:
        AIPolicyRead containing normalized analytics, zones and provider config.
        Secret-like provider keys are removed at every depth, including keys
        stored before nested rejection, so GET and list-shaped callers cannot
        read those values back.
    """
    return AIPolicyRead(
        camera_id=row.camera_id, enabled=row.enabled, source_mode=row.source_mode,
        model_id=row.model_id, stream_role=row.stream_role, sample_fps=row.sample_fps,
        min_confidence=row.min_confidence, analytics=list(row.analytics_json or []),
        zones=list(row.zones_json or []),
        provider_config=redact_provider_config(row.provider_config_json),
        updated_at=row.updated_at,
    )


@router.post("/models", response_model=AIModelRead, status_code=status.HTTP_201_CREATED)
async def create_model(payload: AIModelCreate, session: AsyncSession = Depends(get_session), principal: Principal = Depends(require_roles("admin"))):
    """Register one tenant-scoped AI model version.

    Args:
        payload: Validated AI model metadata.
        session: Database session used for uniqueness checks and persistence.
        principal: Authorized administrator.

    Returns:
        Persisted AIModelRead.

    Raises:
        HTTPException: If scope, artifact/provider validation or uniqueness fails.
    """
    require_scope(principal, payload.tenant_id)
    try:
        artifact_ref = validate_artifact_ref(payload.artifact_ref)
        sha256 = validate_sha256(payload.sha256)
        if payload.provider_type == "onnx":
            if not artifact_ref.startswith("model://"):
                raise ValueError("ONNX models require a model:// artifact_ref")
            if not sha256:
                raise ValueError("ONNX model versions require a SHA-256 identity")
        if payload.provider_type == "external" and not artifact_ref.startswith("provider://"):
            raise ValueError("external providers require a provider:// artifact_ref")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    existing = (await session.execute(select(AIModelEntity).where(
        AIModelEntity.tenant_id == payload.tenant_id,
        AIModelEntity.name == payload.name,
        AIModelEntity.version == payload.version,
    ))).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "AI model version already exists")
    row = AIModelEntity(
        tenant_id=payload.tenant_id, name=payload.name, version=payload.version,
        provider_type=payload.provider_type, artifact_ref=artifact_ref, sha256=sha256,
        labels_json=sorted(set(payload.labels)), input_width=payload.input_width,
        input_height=payload.input_height, enabled=payload.enabled,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return model_read(row)


@router.get("/models", response_model=list[AIModelRead])
async def list_models(enabled: bool | None = None, limit: int = Query(default=200, ge=1, le=1000), session: AsyncSession = Depends(get_session), principal: Principal = Depends(require_roles("admin","operator","viewer"))):
    """List AI model versions visible to the authenticated tenant.

    Args:
        enabled: Optional enabled-state filter.
        limit: Bounded maximum number of rows.
        session: Database session used to query models.
        principal: Authenticated administrator, operator or viewer.

    Returns:
        AIModelRead list ordered newest first.
    """
    q = select(AIModelEntity).order_by(AIModelEntity.created_at.desc()).limit(limit)
    if principal.tenant_id != "*":
        q = q.where(AIModelEntity.tenant_id == principal.tenant_id)
    if enabled is not None:
        q = q.where(AIModelEntity.enabled.is_(enabled))
    return [model_read(row) for row in (await session.execute(q)).scalars().all()]


@router.put("/cameras/{camera_id}/policy", response_model=AIPolicyRead)
async def set_camera_policy(camera_id: str, payload: AIPolicyUpdate, session: AsyncSession = Depends(get_session), principal: Principal = Depends(require_roles("admin","operator"))):
    """Create or update the AI policy for an authorized camera.

    Args:
        camera_id: Camera identifier whose policy is changing.
        payload: Validated policy configuration.
        session: Database session for model/policy lookup and persistence.
        principal: Authorized administrator or operator.

    Returns:
        Persisted AIPolicyRead.

    Raises:
        HTTPException: If camera/model authorization, availability or provider
            configuration validation fails.
    """
    camera = await authorized_camera(session, camera_id, principal)
    model = None
    if payload.source_mode == "inference" and payload.model_id:
        model = await session.get(AIModelEntity, payload.model_id)
        if not model or model.tenant_id != camera.tenant_id:
            raise HTTPException(404, "AI model not found")
        if not model.enabled:
            raise HTTPException(409, "AI model is disabled")
    try:
        provider_config = validate_provider_config(payload.provider_config)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    row = (await session.execute(select(CameraAIPolicyEntity).where(CameraAIPolicyEntity.camera_id == camera.id))).scalar_one_or_none()
    if row is None:
        row = CameraAIPolicyEntity(camera_id=camera.id)
        session.add(row)
    row.enabled = payload.enabled
    row.source_mode = payload.source_mode
    row.model_id = model.id if model else None
    row.stream_role = payload.stream_role
    row.sample_fps = payload.sample_fps
    row.min_confidence = payload.min_confidence
    row.analytics_json = sorted(set(payload.analytics))
    row.zones_json = [zone.model_dump(mode="json") for zone in payload.zones]
    row.provider_config_json = provider_config
    await session.commit()
    await session.refresh(row)
    return policy_read(row)


@router.get("/cameras/{camera_id}/policy", response_model=AIPolicyRead)
async def get_camera_policy(camera_id: str, session: AsyncSession = Depends(get_session), principal: Principal = Depends(require_roles("admin","operator","viewer"))):
    """Return the configured AI policy for an authorized camera.

    Args:
        camera_id: Camera identifier whose policy is requested.
        session: Database session used for camera/policy lookup.
        principal: Authenticated administrator, operator or viewer.

    Returns:
        AIPolicyRead for the camera.

    Raises:
        HTTPException: If camera access fails or no policy is configured.
    """
    camera = await authorized_camera(session, camera_id, principal)
    row = (await session.execute(select(CameraAIPolicyEntity).where(CameraAIPolicyEntity.camera_id == camera.id))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "AI policy not configured")
    return policy_read(row)


@router.post("/results", response_model=AIIngestRead, status_code=status.HTTP_202_ACCEPTED)
async def ingest_ai_result(payload: AIResultIn, session: AsyncSession = Depends(get_session), principal: Principal = Depends(require_roles("service","admin"))):
    """Validate and ingest AI detections for the active camera policy/model.

    Args:
        payload: Camera/model result batch and detections.
        session: Database session used for policy/model validation and event outbox.
        principal: Authorized service or administrator identity.

    Returns:
        AIIngestRead reporting accepted event count.

    Raises:
        HTTPException: If camera scope, policy/model state or result identity is invalid.
    """
    camera = await authorized_camera(session, payload.camera_id, principal)
    policy = (await session.execute(select(CameraAIPolicyEntity).where(CameraAIPolicyEntity.camera_id == camera.id))).scalar_one_or_none()
    if not policy or not policy.enabled or policy.source_mode != "inference":
        raise HTTPException(409, "Camera inference policy is not enabled")
    if policy.model_id != payload.model_id:
        raise HTTPException(409, "Result model does not match current camera AI policy")
    model = await session.get(AIModelEntity, payload.model_id)
    if not model or not model.enabled or model.tenant_id != camera.tenant_id:
        raise HTTPException(409, "AI model is unavailable")
    allowed = set(policy.analytics_json or [])
    filtered = [d for d in payload.detections if d.event_type in allowed and d.confidence >= policy.min_confidence]
    observed_at = payload.observed_at if payload.observed_at.tzinfo else payload.observed_at.replace(tzinfo=timezone.utc)
    accepted = await publish_ai_events(
        session=session,
        camera=camera,
        model=model,
        result_id=payload.result_id,
        observed_at=observed_at,
        detections=filtered,
    )
    await session.commit()
    return AIIngestRead(accepted_events=accepted)


@router.get("/status", response_model=AIStatusRead)
async def ai_status(session: AsyncSession = Depends(get_session), principal: Principal = Depends(require_roles("admin","operator","viewer"))):
    """Return aggregate enabled AI policy counts within caller scope.

    Args:
        session: Database session used for aggregate policy query.
        principal: Authenticated administrator, operator or viewer.

    Returns:
        AIStatusRead containing enabled/inference policy totals.
    """
    q = select(CameraAIPolicyEntity.source_mode, func.count(CameraAIPolicyEntity.id)).join(CameraEntity, CameraEntity.id == CameraAIPolicyEntity.camera_id).where(CameraAIPolicyEntity.enabled.is_(True)).group_by(CameraAIPolicyEntity.source_mode)
    if principal.tenant_id != "*":
        q = q.where(CameraEntity.tenant_id == principal.tenant_id)
    if "*" not in principal.site_ids:
        q = q.where(CameraEntity.site_id.in_(principal.site_ids))
    rows = dict((await session.execute(q)).all())
    inference = int(rows.get("inference",0))
    return AIStatusRead(enabled_policies=inference, inference_policies=inference)
