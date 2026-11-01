"""Socket protocol against real migrated auth/RBAC state, with controlled I/O."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from app.core.rbac import BUILTIN_ROLES
from app.core.security import create_access_token, revoke_access_token
from app.db.models import Camera, Role, User, UserCameraScope, UserRole
from app.events_engine.ws_stream import event_stream
from starlette.datastructures import URL, Headers, QueryParams


class Events:
    def __init__(self):
        self.subscribers = set()

    def subscribe(self, callback):
        self.subscribers.add(callback)

    def unsubscribe(self, callback):
        self.subscribers.discard(callback)

    async def emit(self, message):
        for callback in list(self.subscribers):
            await callback(message)


class Socket:
    def __init__(self, app, *, query="", origin=None):
        self.app = app
        self.client = SimpleNamespace(host="127.0.0.1")
        self.url = URL("ws://testserver/events/stream")
        self.headers = Headers({"origin": origin} if origin else {})
        self.query_params = QueryParams(query)
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def receive(self):
        return await self.incoming.get()

    async def send_json(self, message):
        await self.outgoing.put(message)

    async def close(self, code=1000):
        await self.outgoing.put({"closed": code})

    def auth(self, token):
        self.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(
            {"type": "authenticate", "token": token})})

    async def next(self):
        return await asyncio.wait_for(self.outgoing.get(), 2)


@pytest.fixture
def stream(settings, db_session):
    settings.ws_auth_timeout_s = 0.1
    settings.ws_revalidate_interval_s = 0.05
    settings.ws_send_timeout_s = 0.1
    role = Role(name="stream-role", builtin=False,
                permissions=[p.value for p in BUILTIN_ROLES["viewer"]])
    user = User(username="stream-viewer")
    camera = Camera(name="allowed", host="192.168.50.11", protocol="rtsp")
    db_session.add_all([role, user, camera])
    db_session.flush()
    db_session.add(UserRole(user_id=user.id, role_id=role.id))
    scope = UserCameraScope(user_id=user.id, camera_id=camera.id)
    db_session.add(scope)
    db_session.commit()
    db_session.refresh(user)
    token, claims = create_access_token(user, role.permissions, settings)
    events = Events()
    app = SimpleNamespace(state=SimpleNamespace(container=SimpleNamespace(
        settings=settings, events=events)))
    return SimpleNamespace(app=app, events=events, token=token, claims=claims,
                           user=user, role=role, camera=camera, scope=scope,
                           db=db_session, settings=settings)


async def test_admission_scope_filter_and_disconnect_cleanup(stream):
    socket = Socket(stream.app)
    task = asyncio.create_task(event_stream(socket))
    await asyncio.sleep(0)
    assert not stream.events.subscribers
    socket.auth(stream.token)
    assert (await socket.next())["type"] == "connected"
    await stream.events.emit({"camera_id": "outside", "id": "secret"})
    await stream.events.emit({"camera_id": stream.camera.id, "id": "allowed"})
    assert (await socket.next())["data"]["id"] == "allowed"
    socket.incoming.put_nowait({"type": "websocket.disconnect"})
    await task
    assert not stream.events.subscribers
    assert stream.app.state.event_admission.total == 0


@pytest.mark.parametrize("change", ["revoke", "disable", "lock", "permission"])
async def test_existing_idle_socket_is_revoked(stream, change):
    socket = Socket(stream.app)
    socket.auth(stream.token)
    task = asyncio.create_task(event_stream(socket))
    assert (await socket.next())["type"] == "connected"
    if change == "revoke":
        revoke_access_token(stream.db, stream.claims)
    elif change == "disable":
        stream.user.is_active = False
    elif change == "lock":
        stream.user.is_locked = True
    else:
        stream.role.permissions = []
    stream.db.commit()
    await asyncio.wait_for(asyncio.shield(task), 2)
    while True:
        message = await socket.next()
        if "closed" in message:
            assert message["closed"] == 4403
            break
    await task
    assert not stream.events.subscribers


async def test_scope_change_filters_events_after_revalidation(stream):
    socket = Socket(stream.app)
    socket.auth(stream.token)
    task = asyncio.create_task(event_stream(socket))
    await socket.next()
    stream.db.delete(stream.scope)
    stream.db.commit()
    assert (await socket.next())["type"] == "heartbeat"
    await stream.events.emit({"camera_id": stream.camera.id, "id": "secret"})
    await stream.events.emit({"camera_id": None, "id": "system"})
    while True:
        message = await socket.next()
        if message.get("type") == "event":
            assert message["data"]["id"] == "system"
            break
    socket.incoming.put_nowait({"type": "websocket.disconnect"})
    await task


@pytest.mark.parametrize("frame", [None, {}, {"type": "websocket.receive", "bytes": b"x"},
    {"type": "websocket.receive", "text": "bad-json"},
    {"type": "websocket.receive", "text": "x" * 8193},
    {"type": "websocket.receive", "text": '{"type":"authenticate","token":"bad"}'}])
async def test_no_subscription_for_invalid_auth(stream, frame):
    socket = Socket(stream.app)
    if frame is not None:
        socket.incoming.put_nowait(frame)
    await event_stream(socket)
    assert (await socket.next())["closed"] in {4401, 4403}
    assert not stream.events.subscribers
    assert stream.app.state.event_admission.total == 0


@pytest.mark.parametrize("query,origin", [("token=secret", None), ("", "https://evil.test"),
                                          ("", "null")])
async def test_query_tokens_and_foreign_origins_denied(stream, query, origin):
    socket = Socket(stream.app, query=query, origin=origin)
    await event_stream(socket)
    assert not socket.accepted
    assert (await socket.next())["closed"] == 4403


async def test_connection_limit_includes_unauthenticated_sockets(stream):
    stream.settings.ws_max_connections = 1
    first = Socket(stream.app)
    task = asyncio.create_task(event_stream(first))
    await asyncio.sleep(0)
    second = Socket(stream.app)
    await event_stream(second)
    assert (await second.next())["closed"] == 4429
    assert not second.accepted
    await task
    assert stream.app.state.event_admission.total == 0


async def test_auth_backend_failure_denies_without_exception_text(stream, monkeypatch, caplog):
    def fail(*args):
        raise RuntimeError("SECRET_MUST_NOT_LOG")
    monkeypatch.setattr("app.events_engine.ws_stream.authorize", fail)
    socket = Socket(stream.app)
    socket.auth(stream.token)
    await event_stream(socket)
    assert (await socket.next())["closed"] == 1011
    assert "SECRET_MUST_NOT_LOG" not in caplog.text
    assert not stream.events.subscribers


async def test_authenticated_socket_rejects_extra_frames(stream):
    socket = Socket(stream.app)
    socket.auth(stream.token)
    task = asyncio.create_task(event_stream(socket))
    await socket.next()
    socket.auth(stream.token)
    assert (await socket.next())["closed"] == 1008
    await task


async def test_slow_send_times_out_and_releases_subscription(stream):
    socket = Socket(stream.app)
    socket.auth(stream.token)
    async def blocked_send(message):
        await asyncio.Event().wait()
    socket.send_json = blocked_send
    await asyncio.wait_for(event_stream(socket), 2)
    assert not stream.events.subscribers
    assert stream.app.state.event_admission.total == 0


async def test_expiry_closes_an_existing_connection(stream, monkeypatch):
    import datetime as dt
    socket = Socket(stream.app)
    socket.auth(stream.token)
    task = asyncio.create_task(event_stream(socket))
    await socket.next()
    future = stream.claims.expires_at + dt.timedelta(seconds=1)
    monkeypatch.setattr("app.core.security.utcnow", lambda: future)
    await asyncio.wait_for(task, 2)
    messages = []
    while not socket.outgoing.empty():
        messages.append(socket.outgoing.get_nowait())
    assert messages[-1] == {"closed": 4408}
    assert not stream.events.subscribers


async def test_cancellation_releases_admission_and_subscription(stream):
    socket = Socket(stream.app)
    socket.auth(stream.token)
    task = asyncio.create_task(event_stream(socket))
    await socket.next()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not stream.events.subscribers
    assert stream.app.state.event_admission.total == 0


async def test_production_plaintext_and_unapproved_origin_denied(stream):
    stream.settings.environment = "production"
    socket = Socket(stream.app)
    await event_stream(socket)
    assert not socket.accepted
    assert (await socket.next())["closed"] == 4403


def test_real_asgi_route_authenticates_before_events(stream):
    from app.events_engine.ws_stream import router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    app = FastAPI()
    app.include_router(router)
    app.state.container = stream.app.state.container
    with TestClient(app) as client, client.websocket_connect("/events/stream") as socket:
        socket.send_json({"type": "authenticate", "token": stream.token})
        assert socket.receive_json()["type"] == "connected"
        socket.send_json({"type": "unsupported"})
        with pytest.raises(WebSocketDisconnect) as exc:
            socket.receive_json()
        assert exc.value.code == 1008
    assert not stream.events.subscribers


async def test_real_transport_rejects_oversized_auth_before_subscription(stream):
    import socket as sockets
    import threading

    import uvicorn
    from app.events_engine.ws_stream import WS_TRANSPORT_OPTIONS, router
    from fastapi import FastAPI
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed
    app = FastAPI()
    app.include_router(router)
    app.state.container = stream.app.state.container
    stream.settings.ws_auth_timeout_s = 5
    listener = sockets.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", **WS_TRANSPORT_OPTIONS))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        async with connect(f"ws://127.0.0.1:{port}/events/stream", proxy=None) as socket:
            await socket.send("x" * 8193)
            with pytest.raises(ConnectionClosed) as exc:
                await socket.recv()
            assert exc.value.rcvd.code == 1009
        assert not stream.events.subscribers
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 5)
        listener.close()


async def test_queued_event_is_filtered_after_scope_changes_during_slow_send(stream):
    socket = Socket(stream.app)
    original_send = socket.send_json
    first_send = asyncio.Event()
    release = asyncio.Event()
    async def send(message):
        if message.get("data", {}).get("id") == "first":
            first_send.set()
            await release.wait()
        await original_send(message)
    socket.send_json = send
    stream.settings.ws_send_timeout_s = 1
    socket.auth(stream.token)
    task = asyncio.create_task(event_stream(socket))
    await socket.next()
    await stream.events.emit({"camera_id": stream.camera.id, "id": "first"})
    await asyncio.wait_for(first_send.wait(), 2)
    await stream.events.emit({"camera_id": stream.camera.id, "id": "queued-secret"})
    stream.db.delete(stream.scope)
    stream.db.commit()
    # Advance beyond one policy interval while the first send is suspended.
    await asyncio.sleep(stream.settings.ws_revalidate_interval_s * 2)
    release.set()
    assert (await socket.next())["data"]["id"] == "first"
    await stream.events.emit({"camera_id": None, "id": "barrier"})
    while True:
        message = await socket.next()
        if message.get("type") == "event":
            assert message["data"]["id"] == "barrier"
            break
    socket.incoming.put_nowait({"type": "websocket.disconnect"})
    await task
