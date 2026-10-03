from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, require_roles
from app.db.session import get_session
from app.models.placement import InfrastructureNodeEntity
from app.routers.cameras import authorized_camera, to_read
from app.services.live_access import LiveAccessError, issue_live_access_token, live_access_jwks
from app.services.camera_lifecycle import available_live_roles, main_live_stream_key

router = APIRouter(prefix="/api/v1/live", tags=["live-media"])
internal_router = APIRouter(prefix="/internal/v1/media", tags=["internal-media"])


class LiveAccessRead(BaseModel):
    """Serialize one short-lived, path-scoped live-media grant."""

    camera_id: str
    stream_role: Literal["main", "sub", "third"]
    path: str
    webrtc_url: str
    hls_url: str
    access_token: str
    expires_at: datetime


@router.post("/cameras/{camera_id}/access", response_model=LiveAccessRead)
async def create_live_access(
    camera_id: str,
    stream_role: Literal["live", "main", "sub", "third"] | None = None,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """Mint a short-lived media grant after camera tenant/site authorization.

    Args:
        camera_id: Camera whose live path is requested.
        stream_role: Primary live path or optional third-stream path.
        session: Database session used for camera/node authorization state.
        principal: Authenticated VMS user whose camera scope is enforced.

    Returns:
        Path-scoped bearer grant plus credential-free WebRTC/HLS endpoints.

    Raises:
        HTTPException: If camera/path is unavailable, out of scope, or token
            signing is not configured.
    """
    camera = await authorized_camera(session, camera_id, principal)
    node = (
        await session.get(InfrastructureNodeEntity, camera.media_node_id)
        if camera.media_node_id
        else None
    )
    public = to_read(camera, node)
    roles = available_live_roles(camera)
    requested_role = ("sub" if "sub" in roles else "main") if stream_role in (None, "live") else stream_role
    if requested_role not in roles:
        raise HTTPException(
            409,
            {"code": "LIVE_ROLE_UNAVAILABLE", "message": "Selected live stream is not available"},
        )
    if requested_role == "third":
        path = camera.third_stream_key
        webrtc_url = public.third_webrtc_url
        hls_url = public.third_hls_url
    elif requested_role == "main" and camera.sub_path:
        path = main_live_stream_key(camera)
        webrtc_url = (
            f"{public.webrtc_url.rsplit('/', 1)[0]}/{path}"
            if public.webrtc_url
            else None
        )
        hls_url = (
            f"{public.hls_url.rsplit('/', 1)[0]}/{path}"
            if public.hls_url
            else None
        )
    else:
        path = camera.stream_key
        webrtc_url = public.webrtc_url
        hls_url = public.hls_url
    if not path or not webrtc_url or not hls_url:
        raise HTTPException(
            409,
            {"code": "LIVE_PATH_UNAVAILABLE", "message": "Live media path is not available"},
        )
    try:
        token, expires_at = issue_live_access_token(
            subject=principal.subject,
            tenant_id=camera.tenant_id,
            site_id=camera.site_id,
            camera_id=camera.id,
            stream_key=path,
        )
    except LiveAccessError as exc:
        raise HTTPException(
            503,
            {"code": "LIVE_ACCESS_UNAVAILABLE", "message": "Live media authorization is unavailable"},
        ) from exc
    return LiveAccessRead(
        camera_id=camera.id,
        stream_role=requested_role,
        path=path,
        webrtc_url=webrtc_url,
        hls_url=hls_url,
        access_token=token,
        expires_at=expires_at,
    )


@internal_router.get("/jwks")
async def media_jwks() -> dict:
    """Expose only the public live-view verification key to media nodes.

    Returns:
        JWKS containing the current public RSA verification key.

    Raises:
        HTTPException: HTTP 503 when live-view signing is not configured safely.
    """
    try:
        return live_access_jwks()
    except LiveAccessError as exc:
        raise HTTPException(
            503,
            {"code": "LIVE_ACCESS_UNAVAILABLE", "message": "Live media authorization is unavailable"},
        ) from exc
