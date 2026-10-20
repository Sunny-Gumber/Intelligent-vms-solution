"""User, role, site and API key administration."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from vms_contracts.dto import RoleOut, SiteOut, UserOut
from vms_contracts.enums import Permission

from app.core import audit
from app.core.crypto import hash_token, new_opaque_token
from app.core.deps import AdminPrincipal, Ctr, CurrentPrincipal, DbSession, client_ip
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.rbac import BUILTIN_ROLES
from app.core.security import hash_password, revoke_all_for_user, validate_password_strength
from app.core.timeutil import utcnow, valid_timezone
from app.db.models import (
    ApiKey,
    Camera,
    Role,
    Site,
    User,
    UserCameraScope,
    UserRole,
    UserSiteScope,
)

router = APIRouter(tags=["administration"])


def user_dto(user: User) -> UserOut:
    return UserOut(
        id=user.id, username=user.username, display_name=user.display_name,
        email=user.email, is_active=user.is_active, is_locked=user.is_locked,
        mfa_enabled=user.mfa_enabled, roles=[ur.role.name for ur in user.roles],
        site_ids=[s.site_id for s in user.site_scopes],
        camera_ids=[c.camera_id for c in user.camera_scopes if not c.deny],
        last_login_at=user.last_login_at, created_at=user.created_at)


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=128)
    password: str
    display_name: str | None = None
    email: str | None = None
    roles: list[str] = ["viewer"]
    site_ids: list[str] = []
    camera_ids: list[str] = []
    must_change_password: bool = True


class UserPatch(BaseModel):
    display_name: str | None = None
    email: str | None = None
    is_active: bool | None = None
    unlock: bool | None = None
    roles: list[str] | None = None
    site_ids: list[str] | None = None
    camera_ids: list[str] | None = None
    new_password: str | None = None


class RoleIn(BaseModel):
    name: str
    description: str | None = None
    permissions: list[str]


class SiteIn(BaseModel):
    name: str
    kind: str = "site"
    parent_id: str | None = None
    timezone: str = "UTC"
    address: str | None = None


class ApiKeyIn(BaseModel):
    name: str
    permissions: list[str]
    expires_days: int | None = None
    rate_limit_per_minute: int = 600


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
@router.get("/users", summary="List users")
def list_users(session: DbSession, principal: AdminPrincipal) -> dict:
    rows = session.execute(select(User).order_by(User.username)).scalars().all()
    return {"total": len(rows), "items": [user_dto(u) for u in rows]}


@router.post("/users", response_model=UserOut, status_code=201, summary="Create a user")
def create_user(request: Request, payload: UserIn, session: DbSession, container: Ctr,
                principal: AdminPrincipal) -> UserOut:
    if session.execute(select(User).where(User.username == payload.username)) \
            .scalar_one_or_none():
        raise ConflictError("That username is already taken", code="username_taken")
    validate_password_strength(payload.password, container.settings, payload.username)

    user = User(username=payload.username, display_name=payload.display_name,
                email=payload.email, password_hash=hash_password(payload.password),
                must_change_password=payload.must_change_password,
                password_changed_at=utcnow())
    session.add(user)
    session.flush()

    for role_name in payload.roles:
        role = session.execute(
            select(Role).where(Role.name == role_name)).scalar_one_or_none()
        if role is None:
            raise NotFoundError(f"Role {role_name!r} does not exist",
                                code="role_not_found")
        session.add(UserRole(user_id=user.id, role_id=role.id))
    for site_id in payload.site_ids:
        session.add(UserSiteScope(user_id=user.id, site_id=site_id))
    for camera_id in payload.camera_ids:
        session.add(UserCameraScope(user_id=user.id, camera_id=camera_id, deny=False))

    audit.record(session, action=audit.Action.USER_CREATE, actor_id=principal.user_id,
                 actor_name=principal.username, object_type="user", object_id=user.id,
                 client_ip=client_ip(request),
                 detail={"username": payload.username, "roles": payload.roles})
    session.commit()
    session.refresh(user)
    return user_dto(user)


@router.patch("/users/{user_id}", response_model=UserOut, summary="Update a user")
def update_user(request: Request, user_id: str, payload: UserPatch, session: DbSession,
                container: Ctr, principal: AdminPrincipal) -> UserOut:
    user = session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found", code="user_not_found")

    changes: dict = {}
    if payload.display_name is not None:
        user.display_name = payload.display_name
    if payload.email is not None:
        user.email = payload.email
    if payload.is_active is not None:
        user.is_active = changes["is_active"] = payload.is_active
        if not payload.is_active:
            # Deactivation takes effect immediately, not at token expiry.
            revoke_all_for_user(session, user.id, "deactivated")
            container.sessions.revoke_user_sessions(session, user.id, "deactivated")
    if payload.unlock:
        user.is_locked = False
        user.locked_until = None
        user.failed_attempts = 0
        changes["unlocked"] = True
    if payload.new_password:
        validate_password_strength(payload.new_password, container.settings,
                                   user.username)
        user.password_hash = hash_password(payload.new_password)
        user.must_change_password = True
        user.password_changed_at = utcnow()
        revoke_all_for_user(session, user.id, "password_reset_by_admin")
        changes["password"] = "reset"

    if payload.roles is not None:
        for link in list(user.roles):
            session.delete(link)
        session.flush()
        for role_name in payload.roles:
            role = session.execute(
                select(Role).where(Role.name == role_name)).scalar_one_or_none()
            if role is None:
                raise NotFoundError(f"Role {role_name!r} does not exist",
                                    code="role_not_found")
            session.add(UserRole(user_id=user.id, role_id=role.id))
        changes["roles"] = payload.roles

    if payload.site_ids is not None:
        for scope in list(user.site_scopes):
            session.delete(scope)
        session.flush()
        for site_id in payload.site_ids:
            session.add(UserSiteScope(user_id=user.id, site_id=site_id))
        changes["site_ids"] = payload.site_ids

    if payload.camera_ids is not None:
        for scope in list(user.camera_scopes):
            session.delete(scope)
        session.flush()
        for camera_id in payload.camera_ids:
            session.add(UserCameraScope(user_id=user.id, camera_id=camera_id))
        changes["camera_ids"] = payload.camera_ids

    audit.record(session, action=audit.Action.USER_UPDATE, actor_id=principal.user_id,
                 actor_name=principal.username, object_type="user", object_id=user_id,
                 client_ip=client_ip(request), detail=changes)
    session.commit()
    session.refresh(user)
    return user_dto(user)


@router.delete("/users/{user_id}", status_code=204, summary="Delete a user")
def delete_user(request: Request, user_id: str, session: DbSession, container: Ctr,
                principal: AdminPrincipal) -> Response:
    user = session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found", code="user_not_found")
    if user.id == principal.user_id:
        raise ConflictError("You cannot delete your own account",
                            code="cannot_delete_self")

    is_admin = any(ur.role.name == "administrator" for ur in user.roles)
    if is_admin:
        admin_count = int(session.execute(
            select(func.count()).select_from(UserRole)
            .join(Role, Role.id == UserRole.role_id)
            .where(Role.name == "administrator")).scalar_one())
        if admin_count <= 1:
            raise ConflictError("The last administrator account cannot be deleted",
                                code="last_administrator")

    revoke_all_for_user(session, user.id, "deleted")
    container.sessions.revoke_user_sessions(session, user.id, "deleted")
    session.delete(user)
    audit.record(session, action=audit.Action.USER_DELETE, actor_id=principal.user_id,
                 actor_name=principal.username, object_type="user", object_id=user_id,
                 client_ip=client_ip(request))
    session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------
@router.get("/roles", summary="List roles")
def list_roles(session: DbSession, principal: CurrentPrincipal) -> dict:
    principal.require(Permission.ROLE_MANAGE)
    rows = session.execute(select(Role).order_by(Role.name)).scalars().all()
    return {
        "items": [RoleOut(
            id=r.id, name=r.name, builtin=r.builtin,
            permissions=(list(BUILTIN_ROLES[r.name])
                         if r.builtin and r.name in BUILTIN_ROLES
                         else list(r.permissions or [])),
            description=r.description) for r in rows],
        "available_permissions": [p.value for p in Permission],
    }


@router.post("/roles", status_code=201, summary="Create a custom role")
def create_role(request: Request, payload: RoleIn, session: DbSession,
                principal: CurrentPrincipal) -> dict:
    principal.require(Permission.ROLE_MANAGE)
    known = {p.value for p in Permission}
    unknown = [p for p in payload.permissions if p not in known]
    if unknown:
        raise ValidationError(f"Unknown permissions: {unknown}",
                              code="unknown_permission")
    if session.execute(select(Role).where(Role.name == payload.name)).scalar_one_or_none():
        raise ConflictError("A role with that name exists", code="role_exists")

    role = Role(name=payload.name, description=payload.description,
                permissions=payload.permissions, builtin=False)
    session.add(role)
    session.flush()
    audit.record(session, action=audit.Action.ROLE_CREATE, actor_id=principal.user_id,
                 actor_name=principal.username, object_type="role", object_id=role.id,
                 client_ip=client_ip(request), detail={"name": payload.name})
    session.commit()
    return {"id": role.id, "name": role.name}


# --------------------------------------------------------------------------
# Sites
# --------------------------------------------------------------------------
@router.get("/sites", summary="Site hierarchy")
def list_sites(session: DbSession, principal: CurrentPrincipal) -> dict:
    principal.require(Permission.LIVE_VIEW)
    rows = session.execute(select(Site).order_by(Site.path, Site.name)).scalars().all()
    counts = dict(session.execute(
        select(Camera.site_id, func.count()).group_by(Camera.site_id)).all())
    visible = [s for s in rows
               if principal.may_access_site(s.id, (s.path or "").split("/"))]
    return {"items": [SiteOut(id=s.id, name=s.name, parent_id=s.parent_id, kind=s.kind,
                              path=s.path, timezone=s.timezone,
                              camera_count=int(counts.get(s.id, 0)))
                      for s in visible]}


@router.post("/sites", response_model=SiteOut, status_code=201, summary="Create a site")
def create_site(request: Request, payload: SiteIn, session: DbSession,
                principal: CurrentPrincipal) -> SiteOut:
    principal.require(Permission.SITE_MANAGE)
    if not valid_timezone(payload.timezone):
        raise ValidationError(f"Unknown timezone {payload.timezone!r}",
                              code="invalid_timezone")
    parent_path = "/"
    if payload.parent_id:
        parent = session.get(Site, payload.parent_id)
        if parent is None:
            raise NotFoundError("Parent site not found", code="site_not_found")
        parent_path = parent.path

    site = Site(name=payload.name, kind=payload.kind, parent_id=payload.parent_id,
                timezone=payload.timezone, address=payload.address)
    session.add(site)
    session.flush()
    site.path = f"{parent_path.rstrip('/')}/{site.id}"
    audit.record(session, action="site.create", actor_id=principal.user_id,
                 actor_name=principal.username, object_type="site", object_id=site.id,
                 client_ip=client_ip(request), detail={"name": payload.name})
    session.commit()
    return SiteOut(id=site.id, name=site.name, parent_id=site.parent_id, kind=site.kind,
                   path=site.path, timezone=site.timezone, camera_count=0)


# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------
@router.get("/apikeys", summary="List API keys")
def list_api_keys(session: DbSession, principal: CurrentPrincipal) -> dict:
    principal.require(Permission.API_KEY_MANAGE)
    rows = session.execute(
        select(ApiKey).order_by(ApiKey.created_at.desc())).scalars().all()
    return {"items": [{"id": k.id, "name": k.name, "prefix": k.prefix,
                       "permissions": k.permissions, "expires_at": k.expires_at,
                       "revoked_at": k.revoked_at, "last_used_at": k.last_used_at}
                      for k in rows]}


@router.post("/apikeys", status_code=201, summary="Create an API key")
def create_api_key(request: Request, payload: ApiKeyIn, session: DbSession,
                   container: Ctr, principal: CurrentPrincipal) -> dict:
    principal.require(Permission.API_KEY_MANAGE)
    known = {p.value for p in Permission}
    unknown = [p for p in payload.permissions if p not in known]
    if unknown:
        raise ValidationError(f"Unknown permissions: {unknown}",
                              code="unknown_permission")
    excessive = [p for p in payload.permissions if p not in principal.permissions]
    if excessive:
        raise ValidationError(
            f"An API key cannot exceed your own permissions: {excessive}",
            code="permission_escalation")

    prefix = new_opaque_token(6)[:12]
    raw = f"{prefix}.{new_opaque_token(32)}"
    record = ApiKey(name=payload.name, prefix=prefix,
                    key_hash=hash_token(raw, container.settings.secret_key),
                    user_id=principal.user_id, permissions=payload.permissions,
                    rate_limit_per_minute=payload.rate_limit_per_minute,
                    expires_at=(utcnow() + dt.timedelta(days=payload.expires_days)
                                if payload.expires_days else None))
    session.add(record)
    session.flush()
    audit.record(session, action=audit.Action.APIKEY_CREATE, actor_id=principal.user_id,
                 actor_name=principal.username, object_type="api_key",
                 object_id=record.id, client_ip=client_ip(request),
                 detail={"name": payload.name, "permissions": payload.permissions})
    session.commit()
    return {"id": record.id, "name": record.name, "api_key": raw,
            "warning": "This is the only time the key is shown. Store it securely."}


@router.delete("/apikeys/{key_id}", status_code=204, summary="Revoke an API key")
def revoke_api_key(request: Request, key_id: str, session: DbSession,
                   principal: CurrentPrincipal) -> Response:
    principal.require(Permission.API_KEY_MANAGE)
    record = session.get(ApiKey, key_id)
    if record is None:
        raise NotFoundError("API key not found", code="api_key_not_found")
    record.revoked_at = utcnow()
    audit.record(session, action=audit.Action.APIKEY_REVOKE, actor_id=principal.user_id,
                 actor_name=principal.username, object_type="api_key",
                 object_id=key_id, client_ip=client_ip(request))
    session.commit()
    return Response(status_code=204)
