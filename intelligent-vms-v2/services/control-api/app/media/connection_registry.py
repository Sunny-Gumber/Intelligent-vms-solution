"""Gateway connection bindings and active revocation (ADR-0015 section 6).

Admission (POST /internal/media/auth) only runs when a viewer connects. An
established WebRTC or RTSP session keeps flowing after its live session is
closed, expired, or its user loses permission. This module closes that gap:

  * bind()     records which gateway session belongs to which live session,
               bounded per live session.
  * enforce()  one bounded revocation cycle: decide which bound connections
               must end, then kick exactly those sessions at the gateway.

State lives in the database (media_connections), so a control-API restart
does not lose the ability to disconnect a specific viewer: the next cycle
after startup simply continues.

Targeting is per gateway session id, never per path, so revoking one viewer
cannot disconnect another authorized viewer of the same camera.

HLS is NOT bound: the gateway shares one HLS muxer among all viewers of a
path and exposes no per-viewer session to kick. HLS revocation therefore
depends on the gateway re-authenticating each HLS request, which must be
verified on the pinned release before HLS revocation is claimed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from vms_contracts.enums import Permission

from app.config import Settings, get_settings
from app.core.rbac import build_principal, site_ancestors
from app.core.timeutil import utcnow
from app.db.models import Camera, LiveSession, MediaConnection, User
from app.logging_setup import get_logger
from app.media.mediamtx_client import (
    GatewayError,
    MediaGatewayClient,
    valid_connection_id,
)

log = get_logger("media.connections")

# Protocols whose gateway sessions can be disconnected one at a time.
KICKABLE_PROTOCOLS = frozenset({"webrtc", "rtsp", "rtsps"})


@dataclass
class EnforcementResult:
    marked: int = 0
    kicked: int = 0
    kick_failures: int = 0
    abandoned: int = 0
    purged: int = 0
    reasons: dict[str, int] = field(default_factory=dict)


def user_may_view(session: Session, user: User | None, camera: Camera | None,
                  cache: dict[str, object] | None = None) -> bool:
    """Current, server-side live-view decision for one user and one camera.

    Shared by the admission hook and the revocation cycle so both apply the
    same rule: active, unlocked account; enabled camera; live.view capability;
    camera inside the user's site/camera scope.
    """
    if user is None or camera is None or not camera.enabled:
        return False
    if not user.is_active:
        return False
    if user.is_locked and (user.locked_until is None or user.locked_until > utcnow()):
        return False
    principal = None if cache is None else cache.get(user.id)
    if principal is None:
        principal = build_principal(session, user)
        if cache is not None:
            cache[user.id] = principal
    if not principal.has(Permission.LIVE_VIEW):
        return False
    return principal.may_access_camera(camera.id, camera.site_id,
                                       site_ancestors(session, camera.site_id))


class MediaConnectionRegistry:
    def __init__(self, gateway: MediaGatewayClient,
                 settings: Settings | None = None) -> None:
        self.gateway = gateway
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------
    # Admission-time binding
    # ------------------------------------------------------------------
    def bind(self, session: Session, live: LiveSession, protocol: str,
             connection_id: str) -> MediaConnection:
        """Record (idempotently) that a gateway session belongs to `live`.

        Bounded: when a live session holds more than
        media_connection_max_per_session open bindings (e.g. repeated
        reconnects), the oldest are marked 'superseded' and the revocation
        cycle kicks them.
        """
        if protocol not in KICKABLE_PROTOCOLS:
            raise ValueError(f"protocol {protocol!r} is not bindable")
        if not valid_connection_id(connection_id):
            raise ValueError("invalid gateway connection id")
        now = utcnow()
        existing = session.execute(
            select(MediaConnection).where(
                MediaConnection.protocol == protocol,
                MediaConnection.gateway_conn_id == connection_id)).scalar_one_or_none()
        if existing is not None:
            if existing.live_session_id != live.id:
                # A gateway id re-presented with another session's token.
                raise ValueError("gateway connection id already bound to another session")
            if existing.closed_at is not None or existing.revoke_reason is not None:
                # Re-authentication on a connection already ordered to end.
                raise ValueError("gateway connection has been revoked")
            existing.last_admitted_at = now
            return existing

        binding = MediaConnection(
            live_session_id=live.id, user_id=live.user_id, camera_id=live.camera_id,
            path=live.path, protocol=protocol, gateway_conn_id=connection_id,
            created_at=now, last_admitted_at=now, kick_attempts=0)
        session.add(binding)
        session.flush()

        open_rows = session.execute(
            select(MediaConnection).where(
                MediaConnection.live_session_id == live.id,
                MediaConnection.closed_at.is_(None),
                MediaConnection.revoke_reason.is_(None))
            .order_by(MediaConnection.created_at, MediaConnection.id)).scalars().all()
        overflow = len(open_rows) - self.settings.media_connection_max_per_session
        for row in open_rows[:max(overflow, 0)]:
            if row.id == binding.id:
                continue
            row.revoke_reason = "superseded"
            row.revoked_at = now
        return binding

    # ------------------------------------------------------------------
    # Revocation
    # ------------------------------------------------------------------
    def mark_for_sessions(self, session: Session, live_session_ids: list[str],
                          reason: str) -> int:
        if not live_session_ids:
            return 0
        rows = session.execute(
            select(MediaConnection).where(
                MediaConnection.live_session_id.in_(live_session_ids),
                MediaConnection.closed_at.is_(None),
                MediaConnection.revoke_reason.is_(None))).scalars().all()
        now = utcnow()
        for row in rows:
            row.revoke_reason = reason
            row.revoked_at = now
        return len(rows)

    def _mark_by_policy(self, session: Session, result: EnforcementResult) -> None:
        """Decide which open, not-yet-revoked bindings must end now."""
        now = utcnow()
        batch = self.settings.media_revocation_batch
        rows = session.execute(
            select(MediaConnection, LiveSession)
            .join(LiveSession, LiveSession.id == MediaConnection.live_session_id,
                  isouter=True)
            .where(MediaConnection.closed_at.is_(None),
                   MediaConnection.revoke_reason.is_(None))
            .order_by(MediaConnection.last_policy_check_at.asc().nulls_first(),
                      MediaConnection.last_admitted_at)
            .limit(batch)).all()
        principals: dict[str, object] = {}
        users: dict[str, User | None] = {}
        cameras: dict[str, Camera | None] = {}
        for binding, live in rows:
            reason: str | None = None
            if live is None:
                reason = "session_missing"
            elif live.closed_at is not None:
                reason = f"session_{live.close_reason or 'closed'}"[:64]
            elif live.expires_at <= now:
                reason = "session_expired"
            else:
                # populate_existing: re-read roles/scopes from the database even
                # if this session already holds (possibly stale) copies.
                if binding.user_id not in users:
                    users[binding.user_id] = session.get(User, binding.user_id,
                                                         populate_existing=True)
                if binding.camera_id not in cameras:
                    cameras[binding.camera_id] = session.get(Camera, binding.camera_id,
                                                             populate_existing=True)
                if not user_may_view(session, users[binding.user_id],
                                     cameras[binding.camera_id], principals):
                    reason = "permission_revoked"
                    live.closed_at = now
                    live.close_reason = "permission_revoked"
            if reason is not None:
                binding.revoke_reason = reason
                binding.revoked_at = now
                result.marked += 1
                result.reasons[reason] = result.reasons.get(reason, 0) + 1
            binding.last_policy_check_at = now

    async def _kick_marked(self, session: Session, result: EnforcementResult,
                           only_sessions: list[str] | None = None) -> None:
        stmt = (select(MediaConnection)
                .where(MediaConnection.closed_at.is_(None),
                       MediaConnection.revoke_reason.is_not(None))
                .order_by(MediaConnection.revoked_at)
                .limit(min(self.settings.media_revocation_batch,
                           self.settings.media_revocation_kick_concurrency)))
        if only_sessions is not None:
            stmt = stmt.where(MediaConnection.live_session_id.in_(only_sessions))

        async def kick(binding: MediaConnection) -> Exception | None:
            try:
                await asyncio.wait_for(
                    self.gateway.kick_connection(binding.protocol,
                                                  binding.gateway_conn_id),
                    timeout=self.settings.media_revocation_kick_timeout_s)
            except (GatewayError, ValueError, TimeoutError) as exc:
                return exc
            return None

        bindings = session.execute(stmt).scalars().all()
        errors = await asyncio.gather(*(kick(binding) for binding in bindings))
        for binding, exc in zip(bindings, errors, strict=True):
            if exc is not None:
                binding.kick_attempts += 1
                result.kick_failures += 1
                if binding.kick_attempts >= self.settings.media_kick_max_attempts:
                    # Bounded: stop retrying, but say so loudly. The viewer may
                    # still be connected; operators must see this.
                    binding.closed_at = utcnow()
                    binding.close_reason = "kick_failed"
                    result.abandoned += 1
                    log.error("media.revocation_kick_abandoned",
                              binding_id=binding.id, protocol=binding.protocol,
                              live_session_id=binding.live_session_id,
                              attempts=binding.kick_attempts,
                              error_type=type(exc).__name__)
                continue
            binding.kicked_at = utcnow()
            binding.closed_at = binding.kicked_at
            binding.close_reason = binding.revoke_reason
            result.kicked += 1
            log.info("media.connection_revoked", binding_id=binding.id,
                     protocol=binding.protocol, live_session_id=binding.live_session_id,
                     reason=binding.revoke_reason)

    def _purge(self, session: Session, result: EnforcementResult) -> None:
        cutoff = utcnow() - dt.timedelta(seconds=self.settings.media_connection_retention_s)
        deleted = session.execute(
            delete(MediaConnection).where(MediaConnection.closed_at.is_not(None),
                                          MediaConnection.closed_at < cutoff))
        result.purged = int(deleted.rowcount or 0)

    async def enforce(self, session: Session) -> EnforcementResult:
        """One bounded revocation cycle. Caller owns commit/rollback."""
        result = EnforcementResult()
        self._mark_by_policy(session, result)
        session.flush()
        await self._kick_marked(session, result)
        self._purge(session, result)
        return result

    async def revoke_sessions_now(self, session: Session, live_session_ids: list[str],
                                  reason: str) -> EnforcementResult:
        """Immediate path used by async callers (e.g. SessionBroker.close)."""
        result = EnforcementResult()
        result.marked = self.mark_for_sessions(session, live_session_ids, reason)
        session.flush()
        await self._kick_marked(session, result, only_sessions=live_session_ids)
        return result
