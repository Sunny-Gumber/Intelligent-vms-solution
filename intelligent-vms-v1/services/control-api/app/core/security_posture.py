from urllib.parse import urlsplit

from app.core.config import settings
from app.services.live_access import LiveAccessError, validate_live_signing_key


def _require_https_url(value: str, setting_name: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError(f"{setting_name} must be an absolute HTTPS URL")
    if parsed.username or parsed.password:
        raise RuntimeError(f"{setting_name} must not contain credentials")
    if parsed.fragment:
        raise RuntimeError(f"{setting_name} must not contain a fragment")


def validate_security_posture() -> None:
    """Fail startup when production OIDC requirements are not securely configured.

    Returns:
        None when OIDC enforcement is disabled or all required production trust
        settings are present and use HTTPS.

    Raises:
        RuntimeError: If AUTH_REQUIRE_OIDC is enabled with auth bypass, local
            HS256 trust, missing issuer/audience/JWKS settings, or unsafe URLs.
    """
    if not settings.auth_require_oidc:
        return

    if settings.auth_disabled:
        raise RuntimeError("AUTH_REQUIRE_OIDC cannot be combined with AUTH_DISABLED")
    if (
        settings.auth_browser_session_enabled
        and not settings.auth_browser_session_cookie_secure
    ):
        raise RuntimeError(
            "AUTH_REQUIRE_OIDC requires secure browser-session cookies when browser sessions are enabled"
        )
    if settings.auth_hs256_secret:
        raise RuntimeError("AUTH_REQUIRE_OIDC forbids AUTH_HS256_SECRET")
    if not settings.auth_jwks_url:
        raise RuntimeError("AUTH_REQUIRE_OIDC requires AUTH_JWKS_URL")
    if not settings.auth_issuer:
        raise RuntimeError("AUTH_REQUIRE_OIDC requires AUTH_ISSUER")
    if not settings.auth_audience.strip():
        raise RuntimeError("AUTH_REQUIRE_OIDC requires AUTH_AUDIENCE")
    try:
        validate_live_signing_key()
    except LiveAccessError as exc:
        raise RuntimeError("AUTH_REQUIRE_OIDC requires a valid live-view signing keyring") from exc

    _require_https_url(settings.auth_jwks_url, "AUTH_JWKS_URL")
    _require_https_url(settings.auth_issuer, "AUTH_ISSUER")
