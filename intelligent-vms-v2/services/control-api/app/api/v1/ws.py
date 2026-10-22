"""WebSocket event stream.

Clients subscribe once and receive events as they are processed. The socket is
authenticated with the same access token as the REST API (passed as a query
parameter, because browsers cannot set headers on WebSocket handshakes), and
every message is filtered against the subscriber's camera scope — a viewer
restricted to one site never sees another site's alarms.
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.rbac import accessible_camera_ids, build_principal
from app.core.security import decode_access_token, is_access_token_revoked
from app.db.models import User
from app.db.session import session_scope
from app.logging_setup import get_logger

router = APIRouter(tags=["events"])
log = get_logger("api.ws")


@router.websocket("/events/stream")
async def event_stream(websocket: WebSocket, token: str = Query(...)) -> None:
    container = websocket.app.state.container

    try:
        claims = decode_access_token(token, container.settings)
    except Exception:  # noqa: BLE001
        await websocket.close(code=4401, reason="invalid token")
        return

    with session_scope() as session:
        if is_access_token_revoked(session, claims.jti):
            await websocket.close(code=4401, reason="token revoked")
            return
        user = session.get(User, claims.sub)
        if user is None or not user.is_active:
            await websocket.close(code=4403, reason="account inactive")
            return
        principal = build_principal(session, user)
        allowed = accessible_camera_ids(session, principal)

    allowed_set = None if allowed is None else set(allowed)
    await websocket.accept()
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)

    async def subscriber(message: dict) -> None:
        camera_id = message.get("camera_id")
        if allowed_set is not None and camera_id is not None \
                and camera_id not in allowed_set:
            return
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            # Slow client: drop rather than block the whole event pipeline.
            pass

    container.events.subscribe(subscriber)
    log.info("ws.connected", user_id=principal.user_id)

    try:
        await websocket.send_json({"type": "connected", "user_id": principal.user_id,
                                   "scoped": allowed_set is not None})
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=20.0)
            except TimeoutError:
                await websocket.send_json({"type": "heartbeat"})
                continue
            await websocket.send_json({"type": "event", "data": message})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning("ws.error", error=str(exc))
    finally:
        container.events.unsubscribe(subscriber)
        with contextlib.suppress(Exception):
            await websocket.close()
        log.info("ws.disconnected", user_id=principal.user_id)
