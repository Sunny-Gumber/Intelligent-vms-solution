"""Authentication: password hashing, JWT issuance, revocation, lockout.

Decisions
  * Argon2id for passwords (memory-hard, resists GPU cracking).
  * Access tokens are short-lived JWTs carrying a `jti`. Revocation is REAL:
    the jti is checked against a denylist on every request, so a deactivated
    user loses access immediately rather than at token expiry.
  * Refresh tokens are opaque random strings; only a keyed hash is stored.
    Rotation is enforced, and REUSE OF A ROTATED TOKEN REVOKES THE WHOLE
    FAMILY (standard stolen-token detection).
  * Lockout is windowed, time-bounded and auditable.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.crypto import hash_token, new_opaque_token
from app.core.errors import AccountLockedError, AuthenticationError, ValidationError
from app.core.timeutil import utcnow
from app.db.models import LoginAttempt, RefreshToken, RevokedAccessToken, User
from app.logging_setup import get_logger

log = get_logger("security")

ALGORITHM = "HS256"
ISSUER = "intelligent-vms"

_HASHER_CACHE: dict[tuple[int, int, int], PasswordHasher] = {}


def _get_hasher(settings: Settings | None = None) -> PasswordHasher:
    """Argon2id hasher built from configured cost parameters.

    Cached per parameter set so repeated calls do not rebuild it. Production
    defaults are validated in app/config.py against the OWASP minimum.
    """
    settings = settings or get_settings()
    key = (settings.password_hash_time_cost, settings.password_hash_memory_kib,
           settings.password_hash_parallelism)
    hasher = _HASHER_CACHE.get(key)
    if hasher is None:
        hasher = PasswordHasher(time_cost=key[0], memory_cost=key[1],
                                parallelism=key[2], hash_len=32, salt_len=16)
        _HASHER_CACHE[key] = hasher
    return hasher



COMMON_WEAK = {
    "password", "password1", "admin", "administrator", "12345678", "123456789",
    "qwerty", "letmein", "welcome", "changeme", "intelligentvms", "camera",
}


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return _get_hasher().hash(password)


def verify_password(password: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False
    try:
        return _get_hasher().verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _get_hasher().check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def validate_password_strength(password: str, settings: Settings | None = None,
                               username: str | None = None) -> None:
    settings = settings or get_settings()
    problems: list[str] = []
    if len(password) < settings.password_min_length:
        problems.append(f"must be at least {settings.password_min_length} characters")
    classes = sum([
        any(c.islower() for c in password),
        any(c.isupper() for c in password),
        any(c.isdigit() for c in password),
        any(not c.isalnum() for c in password),
    ])
    if classes < 3:
        problems.append("must mix at least three of: lowercase, uppercase, digits, symbols")
    if password.lower() in COMMON_WEAK:
        problems.append("is a commonly used password")
    if username and username.lower() in password.lower():
        problems.append("must not contain the username")
    if problems:
        raise ValidationError("Password " + "; ".join(problems), code="weak_password")


# --------------------------------------------------------------------------
# JWT access tokens
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class AccessClaims:
    sub: str
    jti: str
    username: str
    roles: tuple[str, ...]
    permissions: tuple[str, ...]
    expires_at: dt.datetime
    token_type: str = "access"  # noqa: S105 - JWT type label, not a token value
    api_key_id: str | None = None

    @property
    def is_expired(self) -> bool:
        return utcnow() >= self.expires_at


def create_access_token(user: User, permissions: list[str],
                        settings: Settings | None = None, *, ttl_s: int | None = None,
                        extra: dict[str, Any] | None = None) -> tuple[str, AccessClaims]:
    settings = settings or get_settings()
    now = utcnow()
    expires = now + dt.timedelta(seconds=ttl_s or settings.access_token_ttl_s)
    jti = new_opaque_token(16)
    role_names = [ur.role.name for ur in user.roles]
    payload: dict[str, Any] = {
        "iss": ISSUER, "sub": user.id, "jti": jti, "username": user.username,
        "roles": role_names, "perms": permissions, "typ": "access",
        "iat": int(now.timestamp()), "nbf": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    if extra:
        payload.update(extra)
    token = jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)
    claims = AccessClaims(sub=user.id, jti=jti, username=user.username,
                          roles=tuple(role_names), permissions=tuple(permissions),
                          expires_at=expires)
    return token, claims


def decode_access_token(token: str, settings: Settings | None = None) -> AccessClaims:
    settings = settings or get_settings()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM],
                             issuer=ISSUER,
                             options={"require": ["exp", "sub", "jti", "iss"]})
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Access token has expired", code="token_expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Access token is invalid", code="token_invalid") from exc

    if payload.get("typ") != "access":
        raise AuthenticationError("Wrong token type", code="token_invalid")

    return AccessClaims(
        sub=payload["sub"], jti=payload["jti"], username=payload.get("username", ""),
        roles=tuple(payload.get("roles", [])), permissions=tuple(payload.get("perms", [])),
        expires_at=dt.datetime.fromtimestamp(payload["exp"], tz=dt.UTC),
        api_key_id=payload.get("api_key_id"))


def revoke_access_token(session: Session, claims: AccessClaims,
                        reason: str = "logout") -> None:
    if session.get(RevokedAccessToken, claims.jti):
        return
    session.add(RevokedAccessToken(jti=claims.jti, user_id=claims.sub,
                                   expires_at=claims.expires_at, reason=reason))


def is_access_token_revoked(session: Session, jti: str) -> bool:
    return session.get(RevokedAccessToken, jti) is not None


def revoke_all_for_user(session: Session, user_id: str, reason: str) -> int:
    """Revoke every refresh token for a user (deactivate/lock/password reset)."""
    now = utcnow()
    rows = session.execute(
        select(RefreshToken).where(RefreshToken.user_id == user_id,
                                   RefreshToken.revoked_at.is_(None))
    ).scalars().all()
    for row in rows:
        row.revoked_at = now
    log.info("auth.revoke_all", user_id=user_id, reason=reason, count=len(rows))
    return len(rows)


def purge_expired_revocations(session: Session) -> int:
    """Denylist rows are only needed until the token would have expired anyway."""
    result = session.execute(
        delete(RevokedAccessToken).where(RevokedAccessToken.expires_at < utcnow()))
    return int(result.rowcount or 0)


# --------------------------------------------------------------------------
# Refresh tokens
# --------------------------------------------------------------------------
def issue_refresh_token(session: Session, user: User, settings: Settings | None = None, *,
                        family_id: str | None = None, user_agent: str | None = None,
                        client_ip: str | None = None) -> str:
    settings = settings or get_settings()
    raw = new_opaque_token(32)
    record = RefreshToken(
        user_id=user.id, token_hash=hash_token(raw, settings.secret_key),
        expires_at=utcnow() + dt.timedelta(seconds=settings.refresh_token_ttl_s),
        user_agent=(user_agent or "")[:256] or None, client_ip=client_ip)
    if family_id:
        record.family_id = family_id
    session.add(record)
    session.flush()
    return raw


def rotate_refresh_token(session: Session, raw_token: str,
                         settings: Settings | None = None, *,
                         user_agent: str | None = None,
                         client_ip: str | None = None) -> tuple[User, str]:
    """Validate and rotate, detecting reuse of an already-rotated token."""
    settings = settings or get_settings()
    token_hash = hash_token(raw_token, settings.secret_key)
    record = session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    ).scalar_one_or_none()

    if record is None:
        raise AuthenticationError("Refresh token is not recognised", code="refresh_invalid")

    if record.revoked_at is not None:
        # Reuse of a rotated token: assume theft and kill the whole family.
        family = session.execute(
            select(RefreshToken).where(RefreshToken.family_id == record.family_id,
                                       RefreshToken.revoked_at.is_(None))
        ).scalars().all()
        for row in family:
            row.revoked_at = utcnow()
        # Commit immediately: this is a security action and must survive the
        # exception raised below (which rolls back the request transaction).
        session.commit()
        log.warning("auth.refresh_reuse_detected", user_id=record.user_id,
                    family_id=record.family_id, revoked=len(family))
        raise AuthenticationError(
            "Refresh token has already been used; the session family was revoked",
            code="refresh_reused")

    if record.expires_at <= utcnow():
        raise AuthenticationError("Refresh token has expired", code="refresh_expired")

    user = session.get(User, record.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Account is not active", code="account_inactive")

    new_raw = new_opaque_token(32)
    new_record = RefreshToken(
        user_id=user.id, token_hash=hash_token(new_raw, settings.secret_key),
        family_id=record.family_id,
        expires_at=utcnow() + dt.timedelta(seconds=settings.refresh_token_ttl_s),
        user_agent=(user_agent or "")[:256] or None, client_ip=client_ip)
    session.add(new_record)
    session.flush()
    record.revoked_at = utcnow()
    record.replaced_by = new_record.id
    return user, new_raw


def revoke_refresh_token(session: Session, raw_token: str,
                         settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    record = session.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_token(raw_token, settings.secret_key))
    ).scalar_one_or_none()
    if record is None or record.revoked_at is not None:
        return False
    record.revoked_at = utcnow()
    return True


# --------------------------------------------------------------------------
# Lockout
# --------------------------------------------------------------------------
def record_login_attempt(session: Session, username: str, succeeded: bool, *,
                         client_ip: str | None = None, reason: str | None = None) -> None:
    session.add(LoginAttempt(username=username[:128], client_ip=client_ip,
                             succeeded=succeeded, reason=reason))


def recent_failures(session: Session, username: str,
                    settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    since = utcnow() - dt.timedelta(seconds=settings.lockout_window_s)
    return int(session.execute(
        select(func.count()).select_from(LoginAttempt).where(
            LoginAttempt.username == username, LoginAttempt.succeeded.is_(False),
            LoginAttempt.at >= since)
    ).scalar_one())


def assert_not_locked(user: User) -> None:
    if not user.is_active:
        raise AuthenticationError("Account is disabled", code="account_inactive")
    if user.is_locked:
        if user.locked_until and user.locked_until <= utcnow():
            return  # expiry cleared by the caller on successful auth
        remaining = int((user.locked_until - utcnow()).total_seconds()) if user.locked_until else 0
        raise AccountLockedError(
            f"Account is locked. Try again in {max(remaining, 0)} seconds.",
            extra={"retry_after_s": max(remaining, 0)})


def apply_failed_attempt(session: Session, user: User | None, username: str,
                         settings: Settings | None = None, *,
                         client_ip: str | None = None,
                         reason: str = "bad_credentials") -> None:
    settings = settings or get_settings()
    record_login_attempt(session, username, False, client_ip=client_ip, reason=reason)
    if user is None:
        return
    user.failed_attempts = (user.failed_attempts or 0) + 1
    if user.failed_attempts >= settings.lockout_threshold:
        user.is_locked = True
        user.locked_until = utcnow() + dt.timedelta(seconds=settings.lockout_duration_s)
        log.warning("auth.account_locked", user_id=user.id, username=user.username,
                    until=user.locked_until.isoformat())


def clear_failures(session: Session, user: User, *, client_ip: str | None = None) -> None:
    user.failed_attempts = 0
    user.is_locked = False
    user.locked_until = None
    user.last_login_at = utcnow()
    record_login_attempt(session, user.username, True, client_ip=client_ip)
