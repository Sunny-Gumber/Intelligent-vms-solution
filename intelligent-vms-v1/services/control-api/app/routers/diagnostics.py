from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, require_roles
from app.db.session import get_session
from app.models.schemas import DiagnosticRead
from app.routers.cameras import authorized_camera
from app.services.diagnostics import DiagnosticError, media_diagnostics

router = APIRouter(prefix="/api/v1/diagnostics", tags=["diagnostics"])


@router.get("/cameras/{camera_id}", response_model=DiagnosticRead)
async def camera_diagnostics(
    camera_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """Sample media diagnostics for an authorized camera.

    Args:
        camera_id: Camera identifier to inspect.
        session: Database session used for camera authorization.
        principal: Authenticated administrator, operator or viewer.

    Returns:
        DiagnosticRead containing current media/RTP counters when available.

    Raises:
        HTTPException: If camera authorization fails or diagnostics are unavailable.
    """
    camera = await authorized_camera(session, camera_id, principal)
    try:
        values = await media_diagnostics.sample(camera.stream_key)
    except DiagnosticError as exc:
        raise HTTPException(503, str(exc)) from exc
    return DiagnosticRead(
        camera_id=camera.id,
        stream_key=camera.stream_key,
        **values,
    )
