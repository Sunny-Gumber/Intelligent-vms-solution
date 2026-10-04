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


def _validate_deployment_profile() -> None:
    """Fail closed when a named reduced deployment profile enables unsupported services."""
    if settings.deployment_profile == "enterprise-distributed":
        return
    if settings.deployment_profile != "windows-small-site":
        raise RuntimeError("Unknown DEPLOYMENT_PROFILE")

    invalid = []
    if settings.kafka_bootstrap_servers:
        invalid.append("KAFKA_BOOTSTRAP_SERVERS")
    if settings.outbox_enabled:
        invalid.append("OUTBOX_ENABLED")
    if settings.event_pipeline_enabled:
        invalid.append("EVENT_PIPELINE_ENABLED")
    if settings.event_history_enabled and not settings.event_local_store_enabled:
        invalid.append("EVENT_HISTORY_ENABLED")
    if not settings.event_history_enabled and settings.event_local_store_enabled:
        invalid.append("EVENT_LOCAL_STORE_ENABLED")
    if settings.alarm_processing_enabled:
        invalid.append("ALARM_PROCESSING_ENABLED")
    if settings.ai_ui_enabled:
        invalid.append("AI_UI_ENABLED")
    if settings.recording_metadata_events_enabled:
        invalid.append("RECORDING_METADATA_EVENTS_ENABLED")
    if settings.placement_execution_enabled:
        invalid.append("PLACEMENT_EXECUTION_ENABLED")
    if settings.clickhouse_url:
        invalid.append("CLICKHOUSE_URL")
    if invalid:
        raise RuntimeError(
            "windows-small-site profile requires disabled enterprise services: "
            + ", ".join(invalid)
        )


def validate_security_posture() -> None:
    """Fail startup when production OIDC requirements are not securely configured.

    Returns:
        None when OIDC enforcement is disabled or all required production trust
        settings are present and use HTTPS.

    Raises:
        RuntimeError: If AUTH_REQUIRE_OIDC is enabled with auth bypass, local
            HS256 trust, missing issuer/audience/JWKS settings, or unsafe URLs.
    """
    _validate_deployment_profile()

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

    if settings.auth_desktop_oidc_enabled:
        if not settings.auth_oidc_client_id.strip():
            raise RuntimeError("AUTH_DESKTOP_OIDC_ENABLED requires AUTH_OIDC_CLIENT_ID")
        if any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in settings.auth_oidc_scopes):
            raise RuntimeError("AUTH_OIDC_SCOPES contains invalid characters")
        scopes = [value for value in settings.auth_oidc_scopes.split(" ") if value]
        if "openid" not in scopes:
            raise RuntimeError("AUTH_DESKTOP_OIDC_ENABLED requires openid scope")
        if any(any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in value) for value in scopes):
            raise RuntimeError("AUTH_OIDC_SCOPES contains invalid characters")
