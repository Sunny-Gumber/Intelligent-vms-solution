"""Bounded first-frame authentication and continuing event authorization.

No bearer credentials in the URL. Proxy-derived client addresses are canonical.
Limits are per ASGI application/process, not cluster-wide (see ADR-0016).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool
from vms_contracts.enums import Permission

from app.core.errors import AuthenticationError
from app.core.rbac import accessible_camera_ids, build_principal
from app.core.security import decode_access_token, is_access_token_revoked
from app.core.timeutil import utcnow
from app.db.models import User
from app.db.session import session_scope
from app.logging_setup import get_logger

router = APIRouter(tags=["events"])
log = get_logger("api.ws")
MAX_AUTH_BYTES = 8192
WS_TRANSPORT_OPTIONS = {
    "ws": "websockets", "ws_max_size": MAX_AUTH_BYTES, "ws_max_queue": 4,
    "ws_per_message_deflate": False,
}


@dataclass
class Admission:
    """Counters are changed without yielding on the application's event loop."""
    peers: dict[str, int] = field(default_factory=dict)
    total: int = 0

    def acquire(self, peer: str, settings) -> bool:
        if (self.total >= settings.ws_max_connections
                or self.peers.get(peer, 0) >= settings.ws_max_connections_per_ip):
            return False
        self.total += 1
        self.peers[peer] = self.peers.get(peer, 0) + 1
        return True

    def release(self, peer: str) -> None:
        self.total -= 1
        count = self.peers[peer] - 1
        if count:
            self.peers[peer] = count
        else:
            del self.peers[peer]


def origin_allowed(socket: WebSocket, settings) -> bool:
    if settings.environment == "production" and socket.url.scheme != "wss":
        return False
    origin = socket.headers.get("origin")
    if origin is None:
        # Native clients may omit Origin; they still need valid first-frame auth.
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path):
        return False
    same_origin = ("https" if socket.url.scheme == "wss" else "http") + "://" + socket.url.netloc
    return origin == same_origin or origin in settings.cors_origins


def authorize(token: str, settings):
    """Fresh transaction on each check; no DB session crosses an await."""
    claims = decode_access_token(token, settings)
    with session_scope() as db:
        if is_access_token_revoked(db, claims.jti):
            raise AuthenticationError("Access denied", code="token_revoked")
        user = db.get(User, claims.sub)
        if (user is None or not user.is_active or
                (user.is_locked and (user.locked_until is None or user.locked_until > utcnow()))):
            raise AuthenticationError("Access denied", code="account_denied")
        principal = build_principal(db, user)
        if not principal.has(Permission.LIVE_VIEW):
            raise AuthenticationError("Access denied", code="permission_denied")
        allowed = accessible_camera_ids(db, principal)
        # Match REST events: camera-less events require LIVE_VIEW, while camera
        # events additionally require current scope. Documented in ADR-0016.
        return claims, None if allowed is None else frozenset(allowed)


@router.websocket("/events/stream")
async def event_stream(websocket: WebSocket) -> None:
    container = websocket.app.state.container
    settings = container.settings
    admission = getattr(websocket.app.state, "event_admission", None)
    if admission is None:
        admission = Admission()
        websocket.app.state.event_admission = admission
    peer = websocket.client.host if websocket.client else "unknown"
    if websocket.query_params or not origin_allowed(websocket, settings):
        await websocket.close(code=4403)
        return
    if not admission.acquire(peer, settings):
        await websocket.close(code=4429)
        return

    subscribed = False
    receive_task = None
    event_task = None
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)
    allowed = frozenset()
    close_code = 1000

    async def subscriber(message: dict) -> None:
        camera_id = message.get("camera_id")
        if allowed is not None and camera_id is not None and camera_id not in allowed:
            return
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(message)

    async def send(message: dict) -> None:
        await asyncio.wait_for(websocket.send_json(message), settings.ws_send_timeout_s)

    try:
        await websocket.accept()
        frame = await asyncio.wait_for(websocket.receive(), settings.ws_auth_timeout_s)
        text = frame.get("text")
        if (not isinstance(text, str) or len(text.encode("utf-8")) > MAX_AUTH_BYTES):
            close_code = 4401
            return
        try:
            auth = json.loads(text)
        except ValueError:
            auth = None
        if (not isinstance(auth, dict) or set(auth) != {"type", "token"}
                or auth["type"] != "authenticate" or not isinstance(auth["token"], str)
                or not auth["token"]):
            close_code = 4401
            return
        token = auth["token"]
        claims, allowed = await run_in_threadpool(authorize, token, settings)
        container.events.subscribe(subscriber)
        subscribed = True
        await send({"type": "connected", "user_id": claims.sub, "scoped": allowed is not None})
        loop = asyncio.get_running_loop()
        next_check = loop.time() + settings.ws_revalidate_interval_s
        receive_task = asyncio.create_task(websocket.receive())
        event_task = asyncio.create_task(queue.get())
        while True:
            if claims.is_expired:
                close_code = 4408
                return
            if loop.time() >= next_check:
                claims, allowed = await run_in_threadpool(authorize, token, settings)
                next_check = loop.time() + settings.ws_revalidate_interval_s
            timeout = min(next_check - loop.time(), (claims.expires_at - utcnow()).total_seconds())
            done, _ = await asyncio.wait({receive_task, event_task}, timeout=max(0, timeout),
                                          return_when=asyncio.FIRST_COMPLETED)
            if receive_task in done:
                # A disconnect cleans up immediately; this server-push protocol
                # permits no additional client messages after authentication.
                disconnected = receive_task.result()["type"] == "websocket.disconnect"
                close_code = 1000 if disconnected else 1008
                return
            if loop.time() >= next_check:
                claims, allowed = await run_in_threadpool(authorize, token, settings)
                next_check = loop.time() + settings.ws_revalidate_interval_s
            if claims.is_expired:
                close_code = 4408
                return
            if event_task in done:
                message = event_task.result()
                camera_id = message.get("camera_id")
                if allowed is None or camera_id is None or camera_id in allowed:
                    await send({"type": "event", "data": message})
                event_task = asyncio.create_task(queue.get())
            else:
                await send({"type": "heartbeat"})
    except AuthenticationError as exc:
        close_code = 4408 if exc.code == "token_expired" else 4403
    except (WebSocketDisconnect, TimeoutError):
        close_code = 4401
    except Exception as exc:  # noqa: BLE001 - authorization failures must close
        close_code = 1011
        log.warning("ws.failed", error_type=type(exc).__name__)
    finally:
        if subscribed:
            container.events.unsubscribe(subscriber)
        tasks = [task for task in (receive_task, event_task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        admission.release(peer)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(websocket.close(code=close_code), settings.ws_send_timeout_s)
