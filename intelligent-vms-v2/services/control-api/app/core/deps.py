"""FastAPI dependencies: authentication, authorization, rate limiting, scoping.

EVERY protected endpoint resolves its principal here. No code path trusts a
client-supplied role, permission or camera list. The chain is:

    bearer token / API key
      -> decode + revocation check
      -> load user, verify active and unlocked
      -> build Principal (roles -> permissions, site/camera scope)
      -> per-permission guard
      -> per-object scope guard (camera/site)
"""

from __future__ import annotations

from typing import Annotated, Iterator

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from vms_contracts.enums import Permission

from app.container import Container
from app.core.crypto import constant_time_equals, hash_token
from app.core.errors import (
    AuthenticationError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
)
from app.core.rbac import Principal, assert_camera_access, build_principal
from app.core.security import decode_access_token, is_access_token_revoked
from app.core.timeutil import utcnow
from app.db.models import ApiKey, Camera, User
from app.db.session import db_dependency
from app.logging_setup import actor_id as actor_context

bearer_scheme = HTTPBearer(auto_error=False)


def get_container(request: Request) -> Container:
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - misconfiguration guard
        raise RuntimeError("application container is not initialised")
    return container


def get_db() -> Iterator[Session]:
    yield from db_dependency()


DbSession = Annotated[Session, Depends(get_db)]
Ctr = Annotated[Container, Depends(get_container)]


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _principal_from_api_key(session: Session, container: Container, raw_key: str,
                            ip: str | None) -> Principal:
    prefix, _, _ = raw_key.partition(".")
    record = session.execute(
        select(ApiKey).where(ApiKey.prefix == prefix)).scalar_one_or_none()
    if record is None or record.revoked_at is not None:
        raise AuthenticationError("API key is not valid", code="api_key_invalid")
    if record.expires_at and record.expires_at <= utcnow():
        raise AuthenticationError("API key has expired", code="api_key_expired")
    if not constant_time_equals(record.key_hash,
                                hash_token(raw_key, container.settings.secret_key)):
        raise AuthenticationError("API key is not valid", code="api_key_invalid")

    user = session.get(User, record.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("The API key owner is not active",
                                  code="account_inactive")

    allowed, retry_after = container.limiters.check("api", f"key:{record.id}")
    if not allowed:
        raise RateLimitedError("API key rate limit exceeded",
                               extra={"retry_after_s": round(retry_after, 1)})

    record.last_used_at = utcnow()
    return build_principal(session, user, api_key_id=record.id,
                           api_key_permissions=record.permissions or [], client_ip=ip)


def current_principal(
    request: Request,
    session: DbSession,
    container: Ctr,
    credentials: Annotated[HTTPAuthorizationCredentials | None,
                           Depends(bearer_scheme)] = None,
    api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> Principal:
    ip = client_ip(request)

    if api_key:
        principal = _principal_from_api_key(session, container, api_key, ip)
        actor_context.set(principal.user_id)
        request.state.principal = principal
        return principal

    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Authentication is required", code="not_authenticated")

    claims = decode_access_token(credentials.credentials, container.settings)
    if is_access_token_revoked(session, claims.jti):
        raise AuthenticationError("This session has been revoked", code="token_revoked")

    user = session.get(User, claims.sub)
    if user is None:
        raise AuthenticationError("Account no longer exists", code="account_missing")
    if not user.is_active:
        raise AuthenticationError("Account is disabled", code="account_inactive")
    if user.is_locked and user.locked_until and user.locked_until > utcnow():
        raise AuthenticationError("Account is locked", code="account_locked")

    allowed, retry_after = container.limiters.check("api", f"user:{user.id}")
    if not allowed:
        raise RateLimitedError("Request rate limit exceeded",
                               extra={"retry_after_s": round(retry_after, 1)})

    principal = build_principal(session, user, client_ip=ip)
    actor_context.set(principal.user_id)
    request.state.principal = principal
    return principal


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def require(*permissions: Permission):
    """Dependency factory enforcing capability."""

    def dependency(principal: CurrentPrincipal) -> Principal:
        principal.require(*permissions)
        return principal

    return dependency


def require_admin(principal: CurrentPrincipal) -> Principal:
    if not principal.is_admin:
        raise PermissionDeniedError("Administrator rights are required",
                                    code="admin_required")
    return principal


AdminPrincipal = Annotated[Principal, Depends(require_admin)]


def load_camera(session: Session, principal: Principal, camera_id: str,
                *permissions: Permission) -> Camera:
    """Fetch a camera and enforce both capability and scope."""
    camera = session.get(Camera, camera_id)
    if camera is None:
        raise NotFoundError("Camera not found", code="camera_not_found")
    assert_camera_access(session, principal, camera, *permissions)
    return camera


def rate_limit(bucket: str, cost: float = 1.0):
    """Per-principal rate limit for expensive operations."""

    def dependency(principal: CurrentPrincipal, container: Ctr) -> None:
        allowed, retry_after = container.limiters.check(bucket, principal.user_id, cost)
        if not allowed:
            raise RateLimitedError(
                f"Too many {bucket} requests. Retry in {retry_after:.0f}s.",
                extra={"retry_after_s": round(retry_after, 1)})

    return dependency
