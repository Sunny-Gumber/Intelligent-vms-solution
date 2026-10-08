import asyncio
import hmac
from dataclasses import dataclass
from typing import Callable

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from app.core.config import settings

bearer = HTTPBearer(auto_error=False)
_jwks_client: PyJWKClient | None = None
BROWSER_SESSION_COOKIE = "vms_session"
BROWSER_CSRF_COOKIE = "vms_csrf"
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(frozen=True)
class Principal:
    """Represent authenticated VMS identity and authorization scope.

    Attributes:
        subject: Stable identity subject from the authentication token.
        roles: Normalized VMS roles granted to the identity.
        tenant_id: Tenant scope, or "*" for global service/admin scope.
        site_ids: Authorized site identifiers, or "*" for all sites.
        node_id: Optional infrastructure-node scope for service identities.

    Raises:
        No exception is raised by normal dataclass construction.
    """

    subject: str
    roles: frozenset[str]
    tenant_id: str
    site_ids: frozenset[str]
    node_id: str | None = None

    def has_any_role(self, *roles: str) -> bool:
        """Return whether the principal owns at least one requested role.

        Args:
            *roles: VMS role names to test.

        Returns:
            True when any requested role is granted; otherwise False.
        """
        return bool(self.roles.intersection(roles))

    def can_access(self, tenant_id: str, site_id: str | None = None) -> bool:
        """Check whether tenant/site scope allows access to a resource.

        Args:
            tenant_id: Resource tenant identifier.
            site_id: Optional resource site identifier.

        Returns:
            True when the principal scope covers the resource; otherwise False.
        """
        if self.tenant_id != "*" and tenant_id != self.tenant_id:
            return False
        if site_id is None or "*" in self.site_ids:
            return True
        return site_id in self.site_ids


def _dev_principal() -> Principal:
    return Principal(
        subject="development-bypass",
        roles=frozenset({"admin", "operator", "viewer", "service"}),
        tenant_id="*",
        site_ids=frozenset({"*"}),
    )


def _decode_token_sync(token: str) -> dict:
    global _jwks_client

    common = {
        "audience": settings.auth_audience,
        "options": {"require": ["exp", "sub"], "verify_iss": bool(settings.auth_issuer)},
    }
    if settings.auth_issuer:
        common["issuer"] = settings.auth_issuer

    if settings.auth_jwks_url:
        if _jwks_client is None:
            _jwks_client = PyJWKClient(settings.auth_jwks_url, cache_keys=True, lifespan=300)
        key = _jwks_client.get_signing_key_from_jwt(token).key
        return jwt.decode(token, key=key, algorithms=["RS256", "RS384", "RS512", "ES256"], **common)

    if settings.auth_hs256_secret:
        return jwt.decode(token, key=settings.auth_hs256_secret, algorithms=["HS256"], **common)

    raise HTTPException(503, "Authentication is enabled but no JWKS URL or local test secret is configured")


def _string_list_claim(claim_value: object, claim_name: str) -> list[str]:
    """Return string entries for one claim, or reject a malformed shape.

    A JSON object decodes to a dict. Iterating that dict yields its keys, so
    {"admin": false} would become the role admin and {"*": false} would become
    every site. Only a string or a list of strings is accepted. Null, numbers,
    booleans, and lists that contain a non-string are refused with HTTP 401
    before a principal exists. Callers must not replace a rejected claim with
    a wildcard.
    """
    if isinstance(claim_value, str):
        return [claim_value]
    if isinstance(claim_value, list) and all(isinstance(item, str) for item in claim_value):
        return claim_value
    raise HTTPException(
        401,
        f"Token {claim_name} claim has an invalid shape",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _principal_from_claims(claims: dict) -> Principal:
    if "roles" in claims:
        raw_roles = claims["roles"]
        role_claim_name = "roles"
    elif "role" in claims:
        raw_roles = claims["role"]
        role_claim_name = "role"
    else:
        raw_roles = []
        role_claim_name = "roles"
    role_names = _string_list_claim(raw_roles, role_claim_name)
    allowed = {"admin", "operator", "viewer", "service"}
    roles = frozenset(name for name in role_names if name in allowed)
    if not roles:
        raise HTTPException(403, "Token has no recognized VMS role")

    tenant_id = str(claims.get("tenant_id", ""))
    if not tenant_id:
        raise HTTPException(403, "Token has no tenant scope")

    if "site_ids" in claims:
        site_names = _string_list_claim(claims["site_ids"], "site_ids")
    else:
        site_names = ["*"]

    return Principal(
        subject=str(claims["sub"]),
        roles=roles,
        tenant_id=tenant_id,
        site_ids=frozenset(site_names),
        node_id=str(claims["node_id"]) if claims.get("node_id") else None,
    )


async def get_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Principal:
    """Resolve the authenticated principal for an API request.

    Args:
        request: Current request used only to attach the authenticated principal
            for bounded security-audit logging.
        credentials: Optional HTTP bearer credentials supplied by FastAPI.

    Returns:
        Development bypass principal when auth is disabled, otherwise the
        validated token principal.

    Raises:
        HTTPException: If credentials are missing, invalid, expired, or cannot
            be validated with configured authentication settings. A malformed
            role or site_ids claim shape is rejected with HTTP 401 before a
            principal is built.
    """
    if settings.auth_disabled:
        principal = _dev_principal()
        request.state.principal = principal
        return principal

    token = ""
    if credentials is not None and credentials.scheme.lower() == "bearer":
        token = credentials.credentials
    elif settings.auth_browser_session_enabled:
        token = request.cookies.get(BROWSER_SESSION_COOKIE, "")
        if token and request.method.upper() in _MUTATING_METHODS:
            csrf_cookie = request.cookies.get(BROWSER_CSRF_COOKIE, "")
            csrf_header = request.headers.get("x-vms-csrf", "")
            if (
                not csrf_cookie
                or not csrf_header
                or not hmac.compare_digest(csrf_cookie, csrf_header)
            ):
                raise HTTPException(403, "Browser session CSRF validation failed")

    if not token:
        raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = await asyncio.to_thread(_decode_token_sync, token)
        principal = _principal_from_claims(claims)
        request.state.principal = principal
        return principal
    except HTTPException:
        raise
    except jwt.PyJWTError as exc:
        raise HTTPException(401, "Invalid or expired bearer token", headers={"WWW-Authenticate": "Bearer"}) from exc


def require_roles(*roles: str) -> Callable:
    """Build a FastAPI dependency enforcing one of the requested roles.

    Args:
        *roles: VMS roles accepted by the protected endpoint.

    Returns:
        Async dependency returning the authenticated principal after role
        validation.

    Raises:
        HTTPException: Raised by the returned dependency when the principal has
            none of the requested roles.
    """
    async def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.has_any_role(*roles):
            raise HTTPException(403, "Insufficient VMS role")
        return principal

    return dependency


def require_scope(principal: Principal, tenant_id: str, site_id: str | None = None) -> None:
    """Enforce tenant/site scope without revealing out-of-scope resources.

    Args:
        principal: Authenticated identity to authorize.
        tenant_id: Resource tenant identifier.
        site_id: Optional resource site identifier.

    Returns:
        None when access is allowed.

    Raises:
        HTTPException: HTTP 404 when the principal is outside resource scope.
    """
    if not principal.can_access(tenant_id, site_id):
        # Do not reveal whether an out-of-scope resource exists.
        raise HTTPException(404, "Resource not found")


def require_node_scope(principal: Principal, node_id: str) -> None:
    """Authorize an administrator or service identity for one node.

    Args:
        principal: Authenticated identity to authorize.
        node_id: Infrastructure node being accessed.

    Returns:
        None when node access is permitted.

    Raises:
        HTTPException: HTTP 403 when the identity cannot operate on the node.
    """
    if principal.has_any_role("admin"):
        return
    if principal.has_any_role("service") and principal.node_id == node_id:
        return
    raise HTTPException(403, "Service token cannot operate on this node")


def require_global_service_scope(principal: Principal) -> None:
    """Authorize infrastructure operations requiring global service scope.

    Args:
        principal: Authenticated identity to authorize.

    Returns:
        None for administrators or globally scoped service identities.

    Raises:
        HTTPException: HTTP 403 when the identity lacks global service scope.
    """
    if principal.has_any_role("admin"):
        return
    if (
        principal.has_any_role("service")
        and principal.node_id is None
        and principal.tenant_id == "*"
        and "*" in principal.site_ids
    ):
        return
    raise HTTPException(403, "Service token cannot run global infrastructure operation")
