"""Role-based access control with site and camera scoping.

A request is permitted when BOTH hold:
  1. Capability: the principal holds the required Permission (from roles, or
     from an API key's list intersected with its owner's permissions).
  2. Scope: the target object is inside the principal's site/camera scope.

Camera scope resolution:
  * Administrators with no scope rows see everything.
  * An explicit per-camera DENY always wins.
  * An explicit per-camera GRANT allows, even outside the site scope.
  * Otherwise the camera's site (or an ancestor) must be in the site scope.
  * A non-admin with no scope at all sees NOTHING (fail closed).

Evaluated server-side in app/core/deps.py for every protected route including
media sessions, playback, export download and PTZ. Client UIs also hide
controls, but that is cosmetic only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from vms_contracts.enums import Permission, Role as RoleName

from app.core.errors import PermissionDeniedError
from app.db.models import Camera, Role, Site, User

VIEWER_PERMISSIONS: tuple[Permission, ...] = (
    Permission.LIVE_VIEW,
    Permission.PLAYBACK_VIEW,
)

OPERATOR_PERMISSIONS: tuple[Permission, ...] = VIEWER_PERMISSIONS + (
    Permission.SNAPSHOT_CAPTURE,
    Permission.RECORDING_MANUAL,
    Permission.PTZ_CONTROL,
    Permission.PTZ_PRESET_MANAGE,
    Permission.EXPORT_CREATE,
    Permission.EXPORT_DOWNLOAD,
    Permission.ALARM_ACKNOWLEDGE,
    Permission.ALARM_RESOLVE,
)

ADMIN_PERMISSIONS: tuple[Permission, ...] = tuple(Permission)

BUILTIN_ROLES: dict[str, tuple[Permission, ...]] = {
    RoleName.VIEWER.value: VIEWER_PERMISSIONS,
    RoleName.OPERATOR.value: OPERATOR_PERMISSIONS,
    RoleName.ADMINISTRATOR.value: ADMIN_PERMISSIONS,
}

# Always additionally written to the privacy access log (Module 9).
PRIVACY_SENSITIVE: frozenset[Permission] = frozenset(
    {Permission.FACE_RECOGNITION_USE, Permission.ANPR_USE})


@dataclass
class Principal:
    """The authenticated caller, resolved once per request."""

    user_id: str
    username: str
    roles: tuple[str, ...]
    permissions: frozenset[str]
    site_ids: frozenset[str] = frozenset()
    camera_grants: frozenset[str] = frozenset()
    camera_denies: frozenset[str] = frozenset()
    is_admin: bool = False
    api_key_id: str | None = None
    client_ip: str | None = None
    _site_closure: frozenset[str] | None = field(default=None, repr=False, compare=False)

    def has(self, permission: Permission | str) -> bool:
        value = permission.value if isinstance(permission, Permission) else permission
        return value in self.permissions

    def require(self, *permissions: Permission | str) -> None:
        missing = [p for p in permissions if not self.has(p)]
        if missing:
            names = ", ".join(p.value if isinstance(p, Permission) else str(p)
                              for p in missing)
            raise PermissionDeniedError(f"Missing required permission(s): {names}",
                                        extra={"required": names})

    @property
    def unrestricted_scope(self) -> bool:
        """True only for admins with no scope restriction configured."""
        return self.is_admin and not self.site_ids and not self.camera_denies

    def may_access_camera(self, camera_id: str, site_id: str | None,
                          site_closure: Iterable[str] | None = None) -> bool:
        if camera_id in self.camera_denies:
            return False
        if camera_id in self.camera_grants:
            return True
        if self.unrestricted_scope:
            return True
        if not self.site_ids:
            return self.is_admin      # non-admin with no scope: fail closed
        if site_id is None:
            return self.is_admin
        if site_id in self.site_ids:
            return True
        return any(a in self.site_ids for a in (site_closure or ()))

    def may_access_site(self, site_id: str | None,
                        site_closure: Iterable[str] | None = None) -> bool:
        if self.unrestricted_scope:
            return True
        if site_id is None:
            return self.is_admin
        if site_id in self.site_ids:
            return True
        return any(a in self.site_ids for a in (site_closure or ()))


def permissions_for_roles(roles: Iterable[Role]) -> frozenset[str]:
    out: set[str] = set()
    for role in roles:
        if role.builtin and role.name in BUILTIN_ROLES:
            out.update(p.value for p in BUILTIN_ROLES[role.name])
        for perm in role.permissions or []:
            out.add(perm.value if isinstance(perm, Permission) else str(perm))
    return frozenset(out)


def build_principal(session: Session, user: User, *, api_key_id: str | None = None,
                    api_key_permissions: Iterable[str] | None = None,
                    client_ip: str | None = None) -> Principal:
    roles = [ur.role for ur in user.roles]
    perms = permissions_for_roles(roles)

    if api_key_permissions is not None:
        # An API key can never exceed its owner's permissions.
        perms = frozenset(perms & frozenset(api_key_permissions))

    return Principal(
        user_id=user.id, username=user.username,
        roles=tuple(r.name for r in roles), permissions=perms,
        site_ids=frozenset(s.site_id for s in user.site_scopes),
        camera_grants=frozenset(s.camera_id for s in user.camera_scopes if not s.deny),
        camera_denies=frozenset(s.camera_id for s in user.camera_scopes if s.deny),
        is_admin=any(r.name == RoleName.ADMINISTRATOR.value for r in roles),
        api_key_id=api_key_id, client_ip=client_ip)


def site_ancestors(session: Session, site_id: str | None) -> list[str]:
    """Return [site_id, parent, grandparent, ...] from the materialized path."""
    if not site_id:
        return []
    site = session.get(Site, site_id)
    if site is None:
        return []
    chain = [p for p in (site.path or "").split("/") if p] or [site_id]
    if site_id not in chain:
        chain.append(site_id)
    return chain


def site_descendants(session: Session, site_id: str) -> list[str]:
    site = session.get(Site, site_id)
    if site is None:
        return []
    prefix = (site.path or "/").rstrip("/") + "/"
    rows = session.execute(select(Site.id).where(Site.path.like(f"{prefix}%"))).scalars().all()
    return [site_id, *rows]


def accessible_camera_ids(session: Session, principal: Principal) -> list[str] | None:
    """Camera id allow-list, or None meaning 'no restriction'.

    An EMPTY list is a legitimate fail-closed outcome meaning "no cameras".
    """
    if principal.unrestricted_scope:
        return None

    expanded: set[str] = set()
    for site_id in principal.site_ids:
        expanded.update(site_descendants(session, site_id))

    ids: set[str] = set(principal.camera_grants)
    if expanded:
        ids.update(session.execute(
            select(Camera.id).where(Camera.site_id.in_(expanded))).scalars().all())
    elif principal.is_admin:
        return None

    return sorted(ids - principal.camera_denies)


def assert_camera_access(session: Session, principal: Principal, camera: Camera,
                         *permissions: Permission) -> None:
    if permissions:
        principal.require(*permissions)
    if not principal.may_access_camera(camera.id, camera.site_id,
                                       site_ancestors(session, camera.site_id)):
        raise PermissionDeniedError("You do not have access to this camera",
                                    extra={"camera_id": camera.id})
