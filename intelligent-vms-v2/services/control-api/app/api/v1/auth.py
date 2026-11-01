"""Authentication endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from vms_contracts.dto import LoginRequest, TokenPair, UserOut

from app.core import audit, mfa
from app.core.deps import Ctr, CurrentPrincipal, DbSession, client_ip
from app.core.errors import AuthenticationError, MfaRequiredError, ValidationError
from app.core.rbac import permissions_for_roles
from app.core.security import (
    apply_failed_attempt,
    assert_not_locked,
    clear_failures,
    create_access_token,
    decode_access_token,
    hash_password,
    issue_refresh_token,
    revoke_access_token,
    revoke_all_for_user,
    revoke_refresh_token,
    rotate_refresh_token,
    validate_password_strength,
    verify_password,
)
from app.core.timeutil import utcnow
from app.db.models import User
from app.logging_setup import get_logger

router = APIRouter(prefix="/auth", tags=["authentication"])
log = get_logger("api.auth")

# A fixed Argon2 hash used to equalise work when a username does not exist, so
# response timing does not reveal which accounts are real. Built lazily because
# the cost parameters come from settings.
_DECOY_HASH: str | None = None


def _decoy_hash() -> str:
    global _DECOY_HASH
    if _DECOY_HASH is None:
        _DECOY_HASH = hash_password("decoy-value-for-constant-time-login")
    return _DECOY_HASH


class RefreshRequest(BaseModel):
    refresh_token: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class MfaVerify(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")


def user_dto(user: User) -> UserOut:
    return UserOut(
        id=user.id, username=user.username, display_name=user.display_name,
        email=user.email, is_active=user.is_active, is_locked=user.is_locked,
        mfa_enabled=user.mfa_enabled, roles=[ur.role.name for ur in user.roles],
        site_ids=[s.site_id for s in user.site_scopes],
        camera_ids=[c.camera_id for c in user.camera_scopes if not c.deny],
        last_login_at=user.last_login_at, created_at=user.created_at)


@router.post("/login", response_model=TokenPair, summary="Authenticate and obtain tokens")
def login(request: Request, payload: LoginRequest, session: DbSession,
          container: Ctr) -> TokenPair:
    ip = client_ip(request)

    allowed, retry_after = container.limiters.check("login", f"{ip}:{payload.username}")
    if not allowed:
        raise AuthenticationError(
            f"Too many sign-in attempts. Try again in {retry_after:.0f} seconds.",
            code="login_rate_limited")

    user = session.execute(
        select(User).where(User.username == payload.username)).scalar_one_or_none()

    if user is None:
        verify_password(payload.password, _decoy_hash())
        apply_failed_attempt(session, None, payload.username, container.settings,
                             client_ip=ip, reason="unknown_user")
        audit.record(session, action=audit.Action.LOGIN_FAILURE,
                     actor_name=payload.username, outcome="failure", client_ip=ip,
                     detail={"reason": "unknown_user"})
        session.commit()
        raise AuthenticationError("Invalid username or password",
                                  code="invalid_credentials")

    assert_not_locked(user)

    if not verify_password(payload.password, user.password_hash):
        apply_failed_attempt(session, user, payload.username, container.settings,
                             client_ip=ip)
        audit.record(session, action=audit.Action.LOGIN_FAILURE, actor_id=user.id,
                     actor_name=user.username, outcome="failure", client_ip=ip,
                     detail={"reason": "bad_password",
                             "failed_attempts": user.failed_attempts})
        session.commit()
        raise AuthenticationError("Invalid username or password",
                                  code="invalid_credentials")

    if user.mfa_enabled:
        if not payload.totp_code:
            raise MfaRequiredError("A six-digit authenticator code is required")
        secret = container.vault.decrypt(user.mfa_secret_blob or "",
                                         aad=f"mfa:{user.id}")
        last_counter = (user.preferences or {}).get("mfa_last_counter")
        ok, counter = mfa.verify_totp(secret, payload.totp_code,
                                      last_counter=last_counter)
        if not ok:
            apply_failed_attempt(session, user, payload.username, container.settings,
                                 client_ip=ip, reason="bad_totp")
            session.commit()
            raise AuthenticationError("The authenticator code is not valid",
                                      code="invalid_totp")
        preferences = dict(user.preferences or {})
        preferences["mfa_last_counter"] = counter
        user.preferences = preferences

    clear_failures(session, user, client_ip=ip)
    permissions = sorted(permissions_for_roles([ur.role for ur in user.roles]))
    access, _ = create_access_token(user, permissions, container.settings)
    refresh = issue_refresh_token(session, user, container.settings,
                                  user_agent=request.headers.get("user-agent"),
                                  client_ip=ip)

    audit.record(session, action=audit.Action.LOGIN_SUCCESS, actor_id=user.id,
                 actor_name=user.username, client_ip=ip,
                 detail={"mfa": user.mfa_enabled})
    session.commit()

    return TokenPair(access_token=access, refresh_token=refresh,
                     expires_in=container.settings.access_token_ttl_s)


@router.post("/refresh", response_model=TokenPair, summary="Rotate a refresh token")
def refresh_tokens(request: Request, payload: RefreshRequest, session: DbSession,
                   container: Ctr) -> TokenPair:
    user, new_refresh = rotate_refresh_token(
        session, payload.refresh_token, container.settings,
        user_agent=request.headers.get("user-agent"), client_ip=client_ip(request))
    permissions = sorted(permissions_for_roles([ur.role for ur in user.roles]))
    access, _ = create_access_token(user, permissions, container.settings)
    audit.record(session, action=audit.Action.TOKEN_REFRESH, actor_id=user.id,
                 actor_name=user.username, client_ip=client_ip(request))
    session.commit()
    return TokenPair(access_token=access, refresh_token=new_refresh,
                     expires_in=container.settings.access_token_ttl_s)


@router.post("/logout", status_code=204, summary="Revoke the current session")
def logout(request: Request, session: DbSession, container: Ctr,
           principal: CurrentPrincipal,
           refresh_token: Annotated[str | None, Body(embed=True)] = None) -> Response:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        claims = decode_access_token(authorization.split(" ", 1)[1], container.settings)
        revoke_access_token(session, claims, reason="logout")
    if refresh_token:
        revoke_refresh_token(session, refresh_token, container.settings)
    container.sessions.revoke_user_sessions(session, principal.user_id, "logout")
    audit.record(session, action=audit.Action.LOGOUT, actor_id=principal.user_id,
                 actor_name=principal.username, client_ip=client_ip(request))
    session.commit()
    return Response(status_code=204)


@router.get("/me", response_model=UserOut, summary="Current user profile")
def me(session: DbSession, principal: CurrentPrincipal) -> UserOut:
    return user_dto(session.get(User, principal.user_id))


@router.get("/me/permissions", summary="Effective permissions of the current user")
def my_permissions(principal: CurrentPrincipal) -> dict:
    return {
        "user_id": principal.user_id,
        "roles": list(principal.roles),
        "permissions": sorted(principal.permissions),
        "is_admin": principal.is_admin,
        "site_scope": sorted(principal.site_ids) or None,
        "camera_grants": sorted(principal.camera_grants) or None,
        "camera_denies": sorted(principal.camera_denies) or None,
    }


@router.post("/password", status_code=204, summary="Change your own password")
def change_password(request: Request, payload: PasswordChange, session: DbSession,
                    container: Ctr, principal: CurrentPrincipal) -> Response:
    user = session.get(User, principal.user_id)
    if not verify_password(payload.current_password, user.password_hash):
        raise AuthenticationError("Your current password is not correct",
                                  code="invalid_credentials")
    validate_password_strength(payload.new_password, container.settings, user.username)
    user.password_hash = hash_password(payload.new_password)
    user.password_changed_at = utcnow()
    user.must_change_password = False
    revoked = revoke_all_for_user(session, user.id, "password_changed")
    audit.record(session, action=audit.Action.PASSWORD_CHANGE, actor_id=user.id,
                 actor_name=user.username, client_ip=client_ip(request),
                 detail={"sessions_revoked": revoked})
    session.commit()
    return Response(status_code=204)


@router.post("/mfa/enroll", summary="Begin TOTP enrolment")
def mfa_enroll(session: DbSession, container: Ctr, principal: CurrentPrincipal) -> dict:
    user = session.get(User, principal.user_id)
    enrolment = mfa.enrollment(user.username)
    preferences = dict(user.preferences or {})
    preferences["mfa_pending_secret"] = container.vault.encrypt(
        enrolment.secret, aad=f"mfa:{user.id}")
    user.preferences = preferences
    session.commit()
    return {"secret": enrolment.secret, "otpauth_uri": enrolment.uri,
            "digits": enrolment.digits, "period": enrolment.period,
            "next_step": "POST /api/v1/auth/mfa/verify with a code from your app"}


@router.post("/mfa/verify", status_code=204, summary="Confirm and activate TOTP")
def mfa_activate(request: Request, payload: MfaVerify, session: DbSession, container: Ctr,
                 principal: CurrentPrincipal) -> Response:
    user = session.get(User, principal.user_id)
    pending = (user.preferences or {}).get("mfa_pending_secret")
    if not pending:
        raise ValidationError("Start enrolment first", code="mfa_not_pending")
    secret = container.vault.decrypt(pending, aad=f"mfa:{user.id}")
    ok, counter = mfa.verify_totp(secret, payload.code)
    if not ok:
        raise AuthenticationError("That code is not valid", code="invalid_totp")

    user.mfa_secret_blob = pending
    user.mfa_enabled = True
    preferences = dict(user.preferences or {})
    preferences.pop("mfa_pending_secret", None)
    preferences["mfa_last_counter"] = counter
    user.preferences = preferences
    audit.record(session, action=audit.Action.MFA_ENABLE, actor_id=user.id,
                 actor_name=user.username, client_ip=client_ip(request))
    session.commit()
    return Response(status_code=204)


@router.post("/mfa/disable", status_code=204, summary="Disable TOTP for your account")
def mfa_disable(request: Request, payload: MfaVerify, session: DbSession, container: Ctr,
                principal: CurrentPrincipal) -> Response:
    user = session.get(User, principal.user_id)
    if not user.mfa_enabled:
        return Response(status_code=204)
    secret = container.vault.decrypt(user.mfa_secret_blob or "", aad=f"mfa:{user.id}")
    if not mfa.verify_totp(secret, payload.code)[0]:
        raise AuthenticationError("That code is not valid", code="invalid_totp")
    user.mfa_enabled = False
    user.mfa_secret_blob = None
    audit.record(session, action=audit.Action.MFA_DISABLE, actor_id=user.id,
                 actor_name=user.username, client_ip=client_ip(request))
    session.commit()
    return Response(status_code=204)
