"""Live view session endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from vms_contracts.dto import LiveSessionOut
from vms_contracts.enums import Permission, StreamRole

from app.core import audit
from app.core.deps import Ctr, CurrentPrincipal, DbSession, client_ip, load_camera
from app.media.session_broker import SessionRequest

router = APIRouter(prefix="/live", tags=["live"])


class OpenSession(BaseModel):
    camera_id: str
    stream_role: StreamRole | None = None
    tile_width: int | None = None
    tile_height: int | None = None


@router.post("/sessions", response_model=LiveSessionOut, status_code=201,
             summary="Open a live view session")
async def open_session(request: Request, payload: OpenSession, session: DbSession,
                       container: Ctr, principal: CurrentPrincipal) -> LiveSessionOut:
    load_camera(session, principal, payload.camera_id, Permission.LIVE_VIEW)
    result = await container.sessions.open(session, SessionRequest(
        camera_id=payload.camera_id, user_id=principal.user_id,
        stream_role=payload.stream_role, tile_width=payload.tile_width,
        tile_height=payload.tile_height, client_ip=client_ip(request)))
    audit.record(session, action=audit.Action.LIVE_SESSION_OPEN,
                 actor_id=principal.user_id, actor_name=principal.username,
                 object_type="camera", object_id=payload.camera_id,
                 client_ip=client_ip(request),
                 detail={"session_id": result.session_id,
                         "role": result.stream_role.value})
    session.commit()
    return result


@router.get("/sessions", summary="Active live sessions")
def list_sessions(session: DbSession, container: Ctr,
                  principal: CurrentPrincipal) -> dict:
    principal.require(Permission.LIVE_VIEW)
    rows = container.sessions.active_sessions(session)
    visible = rows if principal.is_admin else [r for r in rows
                                               if r.user_id == principal.user_id]
    return {"total": len(visible),
            "items": [{"session_id": r.id, "camera_id": r.camera_id,
                       "stream_role": r.stream_role, "user_id": r.user_id,
                       "expires_at": r.expires_at,
                       "estimated_kbps": r.estimated_kbps} for r in visible],
            "server_limit": container.settings.max_live_sessions}


@router.post("/sessions/{session_id}/keepalive", summary="Extend a live session")
def keepalive(session_id: str, session: DbSession, container: Ctr,
              principal: CurrentPrincipal) -> dict:
    expires = container.sessions.keepalive(session, session_id, principal.user_id)
    session.commit()
    return {"session_id": session_id, "expires_at": expires}


@router.delete("/sessions/{session_id}", status_code=204,
               summary="Close a live session")
async def close_session(request: Request, session_id: str, session: DbSession,
                        container: Ctr, principal: CurrentPrincipal) -> Response:
    await container.sessions.close(session, session_id, reason="client_closed",
                                   user_id=principal.user_id)
    audit.record(session, action=audit.Action.LIVE_SESSION_CLOSE,
                 actor_id=principal.user_id, actor_name=principal.username,
                 object_type="live_session", object_id=session_id,
                 client_ip=client_ip(request))
    session.commit()
    return Response(status_code=204)


@router.post("/snapshot/{camera_id}", status_code=201,
             summary="Capture a live snapshot")
async def snapshot(request: Request, camera_id: str, session: DbSession, container: Ctr,
                   principal: CurrentPrincipal) -> dict:
    camera = load_camera(session, principal, camera_id, Permission.SNAPSHOT_CAPTURE)
    record = await container.snapshots.capture_live(session, camera,
                                                    actor_id=principal.user_id)
    audit.record(session, action=audit.Action.SNAPSHOT_TAKE, actor_id=principal.user_id,
                 actor_name=principal.username, object_type="camera",
                 object_id=camera_id, client_ip=client_ip(request),
                 detail={"snapshot_id": record.id})
    session.commit()
    return {"snapshot_id": record.id, "taken_at": record.taken_at,
            "size_bytes": record.size_bytes, "sha256": record.sha256}
