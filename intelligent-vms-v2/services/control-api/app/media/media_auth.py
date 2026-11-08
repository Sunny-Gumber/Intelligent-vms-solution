"""Media gateway admission decision (ADR-0015).

Pure decision logic, separated from the HTTP route so it can be exercised
directly against the real database, broker and RBAC.

Every outcome is ALLOW or DENY. There is no third outcome: any unexpected
condition is a denial. Deny reasons are logged, never returned to the caller.

Credential model
  * Caller (the gateway itself): HTTP Basic credentials on the hook request
    (media_auth_hook_user / media_auth_hook_secret) AND a TCP peer address
    inside media_auth_allowed_callers. The payload `ip` field is the viewer's
    address as reported by the gateway and is never used for trust.
  * Viewer (action=read): the opaque live-session play token, presented in a
    header -- `Authorization: Bearer <token>` (payload.token) or
    `Authorization: Basic <session_id>:<token>` (payload.user/password).
    Credentials in the URL query string are rejected by policy.
  * Control plane (action=api): the configured gateway service credentials.
"""

from __future__ import annotations

import hmac
import ipaddress
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qsl

from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Camera, User
from app.media.connection_registry import (
    KICKABLE_PROTOCOLS,
    MediaConnectionRegistry,
    user_may_view,
)
from app.media.mediamtx_client import valid_connection_id

MAX_TOKEN_LENGTH = 256
MAX_FIELD_LENGTH = 1024
READ_PROTOCOLS = frozenset({"webrtc", "hls", "rtsp", "rtsps"})
URL_CREDENTIAL_KEYS = frozenset({"token", "jwt", "access_token", "pass", "password",
                                 "user", "username", "play_token", "auth"})


class Action(StrEnum):
    READ = "read"
    API = "api"


class DenyReason(StrEnum):
    CONFIG_INVALID = "config_invalid"
    UNTRUSTED_CALLER = "untrusted_caller"
    MALFORMED = "malformed_request"
    ACTION_NOT_PERMITTED = "action_not_permitted"
    SERVICE_CREDENTIALS = "service_credentials_invalid"
    PROTOCOL_NOT_EXPOSED = "protocol_not_exposed"
    URL_CREDENTIAL = "url_credential_rejected"
    NO_CREDENTIALS = "no_credentials"
    SESSION_REJECTED = "session_invalid_expired_closed_or_wrong_path"
    SESSION_MISMATCH = "session_user_mismatch"
    USER_INACTIVE = "user_inactive"
    CAMERA_UNAVAILABLE = "camera_unavailable"
    PERMISSION_REVOKED = "permission_revoked"
    NO_CONNECTION_ID = "no_connection_id"
    CONNECTION_REBOUND = "connection_id_rebound"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: DenyReason | None = None
    live_session_id: str | None = None
    bound: bool = False

    @staticmethod
    def deny(reason: DenyReason) -> Decision:
        return Decision(allowed=False, reason=reason)


@dataclass(frozen=True)
class AuthPayload:
    user: str
    password: str
    token: str
    ip: str
    action: str
    path: str
    protocol: str
    id: str
    query: str

    @staticmethod
    def parse(raw: Any) -> AuthPayload | None:
        """Strictly parse the gateway payload; None means malformed."""
        if not isinstance(raw, dict):
            return None
        values: dict[str, str] = {}
        for key in ("user", "password", "token", "ip", "action", "path", "protocol",
                    "id", "query"):
            value = raw.get(key, "")
            if value is None:
                value = ""
            if not isinstance(value, str) or len(value) > MAX_FIELD_LENGTH:
                return None
            values[key] = value
        return AuthPayload(**values)


def _secret_equals(presented: str, expected: str | None) -> bool:
    if not expected:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def caller_is_trusted(settings: Settings, peer_host: str | None,
                      basic: tuple[str, str] | None) -> bool:
    """Authenticate the hook caller. Payload content is never consulted."""
    if basic is None or peer_host is None:
        return False
    try:
        peer = ipaddress.ip_address(peer_host)
    except ValueError:
        return False
    in_network = False
    for entry in settings.media_auth_allowed_callers:
        try:
            if peer in ipaddress.ip_network(entry, strict=False):
                in_network = True
                break
        except ValueError:
            continue
    if not in_network:
        return False
    user, password = basic
    user_ok = _secret_equals(user, settings.media_auth_hook_user)
    pass_ok = _secret_equals(password, settings.media_auth_hook_secret)
    return user_ok and pass_ok


def _query_carries_credentials(query: str) -> bool:
    if not query:
        return False
    try:
        pairs = parse_qsl(query, keep_blank_values=True)
    except ValueError:
        return True
    return any(key.strip().lower() in URL_CREDENTIAL_KEYS for key, _ in pairs)


def decide(db: Session, *, settings: Settings, broker: Any,
           registry: MediaConnectionRegistry, payload: AuthPayload) -> Decision:
    """Admission decision for an already-authenticated gateway caller."""
    if payload.action == Action.API.value:
        user_ok = _secret_equals(payload.user, settings.media_gateway_user)
        pass_ok = _secret_equals(payload.password, settings.media_gateway_password)
        if user_ok and pass_ok:
            return Decision(allowed=True)
        return Decision.deny(DenyReason.SERVICE_CREDENTIALS)

    # publish: recording pulls cameras directly and paths pull on demand, so
    # nothing legitimate publishes. playback/metrics/pprof are not offered.
    if payload.action != Action.READ.value:
        return Decision.deny(DenyReason.ACTION_NOT_PERMITTED)
    if payload.protocol not in READ_PROTOCOLS:
        return Decision.deny(DenyReason.PROTOCOL_NOT_EXPOSED)
    if _query_carries_credentials(payload.query):
        return Decision.deny(DenyReason.URL_CREDENTIAL)

    token = payload.token or payload.password
    if not token:
        # Normal for RTSP: clients send credentials only after a 401.
        return Decision.deny(DenyReason.NO_CREDENTIALS)
    if len(token) > MAX_TOKEN_LENGTH:
        return Decision.deny(DenyReason.MALFORMED)

    live = broker.verify_play_token(db, payload.path, token)
    if live is None:
        return Decision.deny(DenyReason.SESSION_REJECTED)
    if payload.user and not payload.token and payload.user != live.id:
        return Decision.deny(DenyReason.SESSION_MISMATCH)

    user = db.get(User, live.user_id, populate_existing=True)
    if user is None or not user.is_active:
        return Decision.deny(DenyReason.USER_INACTIVE)
    camera = db.get(Camera, live.camera_id, populate_existing=True)
    if camera is None or not camera.enabled:
        return Decision.deny(DenyReason.CAMERA_UNAVAILABLE)
    if not user_may_view(db, user, camera):
        return Decision.deny(DenyReason.PERMISSION_REVOKED)

    if payload.protocol in KICKABLE_PROTOCOLS:
        if not payload.id:
            # Without a gateway session id this viewer could never be revoked.
            return Decision.deny(DenyReason.NO_CONNECTION_ID)
        if not valid_connection_id(payload.id):
            return Decision.deny(DenyReason.MALFORMED)
        try:
            registry.bind(db, live, payload.protocol, payload.id)
        except ValueError:
            return Decision.deny(DenyReason.CONNECTION_REBOUND)
        return Decision(allowed=True, live_session_id=live.id, bound=True)
    return Decision(allowed=True, live_session_id=live.id, bound=False)
