import secrets

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials

from app.core.auth import (
    BROWSER_CSRF_COOKIE,
    BROWSER_SESSION_COOKIE,
    Principal,
    bearer,
    get_principal,
)
from app.core.config import settings


router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.get("/capabilities")
async def authentication_capabilities():
    """Return non-secret authentication modes supported by this VMS deployment."""
    oidc_enabled = bool(
        settings.auth_desktop_oidc_enabled
        and settings.auth_require_oidc
        and settings.auth_issuer.strip()
        and settings.auth_oidc_client_id.strip()
    )
    return {
        "authentication_required": not settings.auth_disabled,
        "manual_token_login": not settings.auth_require_oidc,
        "remember_session": True,
        "oidc": {
            "enabled": oidc_enabled,
            "required": bool(settings.auth_require_oidc),
            "authority": settings.auth_issuer if oidc_enabled else "",
            "client_id": settings.auth_oidc_client_id if oidc_enabled else "",
            "scopes": settings.auth_oidc_scopes.split() if oidc_enabled else [],
            "callback": "loopback",
            "pkce_methods": ["S256"] if oidc_enabled else [],
        },
    }


def _session_payload(principal: Principal) -> dict:
    return {
        "authenticated": True,
        "browser_session_enabled": settings.auth_browser_session_enabled,
        "roles": sorted(principal.roles),
        "tenant_id": principal.tenant_id,
        "site_ids": sorted(principal.site_ids),
    }


@router.get("/session")
async def browser_session_status(
    principal: Principal = Depends(get_principal),
):
    """Return current browser/API authentication status without exposing credentials."""
    return _session_payload(principal)


@router.post("/session")
async def create_browser_session(
    response: Response,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    principal: Principal = Depends(get_principal),
):
    """Exchange an already-valid bearer token for a same-origin browser session."""
    if not settings.auth_browser_session_enabled:
        raise HTTPException(409, "Browser session exchange is not enabled")
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "Bearer token required")

    csrf = secrets.token_urlsafe(32)
    common = {
        "secure": settings.auth_browser_session_cookie_secure,
        "samesite": "strict",
        "max_age": settings.auth_browser_session_max_age_seconds,
    }
    response.set_cookie(
        BROWSER_SESSION_COOKIE,
        credentials.credentials,
        httponly=True,
        path="/api",
        **common,
    )
    # The UI is served at "/", so the non-secret double-submit CSRF cookie
    # must be visible to that page while the bearer session remains /api-only.
    response.set_cookie(
        BROWSER_CSRF_COOKIE,
        csrf,
        httponly=False,
        path="/",
        **common,
    )
    return _session_payload(principal)


@router.delete("/session")
async def delete_browser_session(
    response: Response,
    principal: Principal = Depends(get_principal),
):
    """Clear same-origin browser session cookies after authenticated CSRF validation."""
    del principal
    response.delete_cookie(
        BROWSER_SESSION_COOKIE,
        path="/api",
        secure=settings.auth_browser_session_cookie_secure,
        samesite="strict",
    )
    response.delete_cookie(
        BROWSER_CSRF_COOKIE,
        path="/",
        secure=settings.auth_browser_session_cookie_secure,
        samesite="strict",
    )
    return {"authenticated": False}
