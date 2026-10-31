"""Live session broker.

Responsibilities
  * Authorize a viewer for one camera + one stream role
  * Enforce global and per-user session caps (bounded allocation)
  * Program the media gateway path on demand
  * Mint a short-lived play token; only its hash is stored
  * Reap expired/idle sessions and tear down unused gateway paths

Guarantees verified by tests:
  * The camera's source URL and credentials never appear in the returned DTO
  * A failure for one camera does not invalidate other sessions
  * Sessions always expire; a crashed client cannot hold a slot forever
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from vms_contracts.dto import LiveSessionOut
from vms_contracts.enums import StreamRole

from app.config import Settings, get_settings
from app.core.crypto import hash_token, new_opaque_token
from app.core.errors import (
    CapacityError,
    DependencyUnavailableError,
    MediaUnavailableError,
    NotFoundError,
    ValidationError,
)
from app.core.timeutil import utcnow
from app.db.models import Camera, CameraStream, LiveSession
from app.logging_setup import get_logger
from app.media.connection_registry import MediaConnectionRegistry
from app.media.mediamtx_client import GatewayError, MediaGatewayClient
from app.media.stream_policy import StreamOption, select_stream
from app.media.url_builder import gateway_path

log = get_logger("media.sessions")


@dataclass
class SessionRequest:
    camera_id: str
    user_id: str
    stream_role: StreamRole | None = None
    tile_width: int | None = None
    tile_height: int | None = None
    client_ip: str | None = None


class SessionBroker:
    def __init__(self, gateway: MediaGatewayClient, credential_store,  # noqa: ANN001
                 settings: Settings | None = None) -> None:
        self.gateway = gateway
        self.credentials = credential_store
        self.settings = settings or get_settings()
        # Per-viewer gateway bindings and active revocation (ADR-0015 s6).
        self.connections = MediaConnectionRegistry(gateway, self.settings)

    def _count_active(self, session: Session, user_id: str | None = None) -> int:
        stmt = select(func.count()).select_from(LiveSession).where(
            LiveSession.closed_at.is_(None), LiveSession.expires_at > utcnow())
        if user_id:
            stmt = stmt.where(LiveSession.user_id == user_id)
        return int(session.execute(stmt).scalar_one())

    def _resolve_stream(self, camera: Camera, request: SessionRequest) -> CameraStream:
        streams = [s for s in camera.streams if s.enabled]
        if not streams:
            raise MediaUnavailableError(
                "This camera has no enabled stream. Re-run camera validation.",
                code="no_stream_configured")

        options = [StreamOption(role=StreamRole(s.role), width=s.width, height=s.height,
                                bitrate_kbps=s.bitrate_kbps, enabled=s.enabled)
                   for s in streams]
        try:
            selection = select_stream(options, tile_width=request.tile_width,
                                      tile_height=request.tile_height,
                                      override=request.stream_role)
        except ValueError as exc:
            raise ValidationError(str(exc), code="stream_not_available") from exc

        stream = next(s for s in streams if s.role == selection.role.value)
        stream._selection_reason = selection.reason        # type: ignore[attr-defined]
        stream._estimated_kbps = selection.estimated_kbps  # type: ignore[attr-defined]
        return stream

    def _source_url(self, camera: Camera, stream: CameraStream) -> str:
        """Build the gateway's upstream URL. NEVER returned to a client."""
        from app.media.url_builder import build_rtsp_url, with_credentials

        if stream.source_url:
            url = (self.credentials.decrypt_stream_url(camera.id, stream.source_url)
                   if stream.source_url_encrypted else stream.source_url)
        else:
            url = build_rtsp_url(host=camera.host, port=camera.rtsp_port, path="/")
        creds = self.credentials.get(camera)
        return with_credentials(url, creds.username, creds.password)

    async def open(self, session: Session, request: SessionRequest) -> LiveSessionOut:
        camera = session.get(Camera, request.camera_id)
        if camera is None:
            raise NotFoundError("Camera not found", code="camera_not_found")
        if not camera.enabled:
            raise MediaUnavailableError("Camera is disabled", code="camera_disabled")

        if self._count_active(session) >= self.settings.max_live_sessions:
            raise CapacityError(
                f"Server live-session limit reached "
                f"({self.settings.max_live_sessions}). Close a view and retry.",
                code="live_session_limit")
        if self._count_active(session, request.user_id) >= \
                self.settings.max_live_sessions_per_user:
            raise CapacityError(
                f"Your live-session limit of "
                f"{self.settings.max_live_sessions_per_user} is reached.",
                code="user_session_limit")

        stream = self._resolve_stream(camera, request)
        path = gateway_path(camera.id, stream.role)

        try:
            await self.gateway.ensure_path(
                path, self._source_url(camera, stream),
                transport="udp" if stream.transport.endswith("udp") else "tcp")
        except GatewayError as exc:
            log.error("live.gateway_unavailable", camera_id=camera.id, error=str(exc))
            raise DependencyUnavailableError(
                "The media gateway is not available, so live video cannot start. "
                "Recording and playback are unaffected.",
                code="media_gateway_unavailable") from exc

        raw_token = new_opaque_token(24)
        expires = utcnow() + dt.timedelta(seconds=self.settings.live_session_ttl_s)
        record = LiveSession(
            camera_id=camera.id, stream_role=stream.role, user_id=request.user_id,
            path=path, token_hash=hash_token(raw_token, self.settings.secret_key),
            client_ip=request.client_ip,
            estimated_kbps=getattr(stream, "_estimated_kbps", None), expires_at=expires)
        session.add(record)
        session.flush()

        log.info("live.session_opened", camera_id=camera.id, session_id=record.id,
                 role=stream.role, reason=getattr(stream, "_selection_reason", ""),
                 user_id=request.user_id)

        webrtc = self.settings.media_public_webrtc_url.rstrip("/")
        hls = self.settings.media_public_hls_url.rstrip("/")
        return LiveSessionOut(
            session_id=record.id, camera_id=camera.id,
            stream_role=StreamRole(stream.role), path=path,
            whep_url=f"{webrtc}/{path}/whep", hls_url=f"{hls}/{path}/index.m3u8",
            play_token=raw_token, expires_at=expires,
            estimated_kbps=record.estimated_kbps)

    def keepalive(self, session: Session, session_id: str, user_id: str) -> dt.datetime:
        record = session.get(LiveSession, session_id)
        if record is None or record.closed_at is not None or record.user_id != user_id:
            raise NotFoundError("Live session not found", code="session_not_found")
        record.last_seen_at = utcnow()
        record.expires_at = utcnow() + dt.timedelta(
            seconds=self.settings.live_session_ttl_s)
        return record.expires_at

    def verify_play_token(self, session: Session, path: str,
                          token: str) -> LiveSession | None:
        return session.execute(
            select(LiveSession).where(
                LiveSession.token_hash == hash_token(token, self.settings.secret_key),
                LiveSession.path == path, LiveSession.closed_at.is_(None),
                LiveSession.expires_at > utcnow())).scalar_one_or_none()

    async def close(self, session: Session, session_id: str, *,
                    reason: str = "client_closed", user_id: str | None = None) -> bool:
        record = session.get(LiveSession, session_id)
        if record is None or record.closed_at is not None:
            return False
        if user_id and record.user_id != user_id:
            return False
        record.closed_at = utcnow()
        record.close_reason = reason
        # Disconnect THIS session's gateway connections now; other viewers of
        # the same path are untouched. Failures are retried by the
        # revocation loop, which also covers callers that cannot await.
        await self.connections.revoke_sessions_now(session, [session_id],
                                                   f"session_{reason}"[:64])
        await self._maybe_release_path(session, record.path)
        log.info("live.session_closed", session_id=session_id, reason=reason)
        return True

    async def close_for_camera(self, session: Session, camera_id: str,
                               reason: str = "camera_changed") -> int:
        rows = session.execute(
            select(LiveSession).where(LiveSession.camera_id == camera_id,
                                      LiveSession.closed_at.is_(None))).scalars().all()
        for record in rows:
            record.closed_at = utcnow()
            record.close_reason = reason
        for path in {r.path for r in rows}:
            await self._force_release_path(path)
        return len(rows)

    async def reap_expired(self, session: Session) -> int:
        """Close timed-out sessions and release idle gateway paths."""
        now = utcnow()
        expired = session.execute(
            select(LiveSession).where(LiveSession.closed_at.is_(None),
                                      LiveSession.expires_at <= now)).scalars().all()
        for record in expired:
            record.closed_at = now
            record.close_reason = "expired"
        for path in {r.path for r in expired}:
            await self._maybe_release_path(session, path)
        if expired:
            log.info("live.sessions_reaped", count=len(expired))
        return len(expired)

    async def _maybe_release_path(self, session: Session, path: str) -> None:
        remaining = int(session.execute(
            select(func.count()).select_from(LiveSession).where(
                LiveSession.path == path, LiveSession.closed_at.is_(None),
                LiveSession.expires_at > utcnow())).scalar_one())
        if remaining == 0:
            await self._force_release_path(path)

    async def _force_release_path(self, path: str) -> None:
        try:
            await self.gateway.kick_readers(path)
            await self.gateway.remove_path(path)
        except GatewayError as exc:
            log.warning("live.path_release_failed", path=path, error=str(exc))

    def active_sessions(self, session: Session) -> list[LiveSession]:
        return list(session.execute(
            select(LiveSession).where(
                LiveSession.closed_at.is_(None),
                LiveSession.expires_at > utcnow())).scalars().all())

    def revoke_user_sessions(self, session: Session, user_id: str,
                             reason: str = "access_revoked") -> int:
        result = session.execute(
            update(LiveSession)
            .where(LiveSession.user_id == user_id, LiveSession.closed_at.is_(None))
            .values(closed_at=utcnow(), close_reason=reason))
        return int(result.rowcount or 0)
