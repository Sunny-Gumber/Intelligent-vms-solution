import base64
import binascii
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app.core.config import settings

LIVE_ACCESS_AUDIENCE = "intelligent-vms-live"
LIVE_ACCESS_ISSUER = "intelligent-vms-control"


class LiveAccessError(ValueError):
    """Represent invalid or unavailable live-media authorization."""


MAX_VERIFICATION_KEYS = 4


def _key_id(key: rsa.RSAPublicKey) -> str:
    public_der = key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(public_der).hexdigest()[:32]


def _signing_key() -> tuple[rsa.RSAPrivateKey, str]:
    encoded = settings.live_view_token_private_key_b64.strip()
    if not encoded:
        raise LiveAccessError("Live-view token signing is not configured")
    try:
        pem = base64.b64decode(encoded, validate=True)
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise LiveAccessError("Live-view token signing is not configured") from exc
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise LiveAccessError("Live-view token signing key must be RSA 2048 bits or stronger")
    return key, _key_id(key.public_key())


def _verification_keys() -> list[tuple[rsa.RSAPublicKey, str]]:
    raw = settings.live_view_token_verification_public_keys_b64_json.strip() or "[]"
    try:
        encoded_keys = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LiveAccessError("Live-view verification keyring must be valid JSON") from exc
    if not isinstance(encoded_keys, list) or not all(isinstance(item, str) for item in encoded_keys):
        raise LiveAccessError("Live-view verification keyring must be a JSON string array")
    if len(encoded_keys) > MAX_VERIFICATION_KEYS:
        raise LiveAccessError("Live-view verification keyring exceeds the supported overlap")
    keys = []
    for encoded in encoded_keys:
        try:
            pem = base64.b64decode(encoded, validate=True)
            key = serialization.load_pem_public_key(pem)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise LiveAccessError("Live-view verification keyring contains an invalid public key") from exc
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            raise LiveAccessError("Live-view verification keys must be public RSA 2048-bit keys or stronger")
        keys.append((key, _key_id(key)))
    return keys


def _verification_keyring() -> list[tuple[rsa.RSAPublicKey, str]]:
    signing_key, signing_kid = _signing_key()
    keys = [(signing_key.public_key(), signing_kid), *_verification_keys()]
    kids = [kid for _key, kid in keys]
    if len(kids) != len(set(kids)):
        raise LiveAccessError("Live-view verification keyring contains a duplicate kid")
    return keys


def validate_live_signing_key() -> None:
    """Validate the configured asymmetric live-view signing key.

    Raises:
        LiveAccessError: If the configured key is missing, malformed or weak.
    """
    _verification_keyring()


def live_access_jwks() -> dict:
    """Return the public JWKS used by MediaMTX to verify live grants.

    Returns:
        JWKS containing the active signing key and bounded overlap verification keys.

    Raises:
        LiveAccessError: If the private signing key is not configured safely.
    """
    keys = []
    for key, kid in _verification_keyring():
        jwk = RSAAlgorithm.to_jwk(key, as_dict=True)
        jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
        keys.append(jwk)
    return {"keys": keys}


def issue_live_access_token(
    *,
    subject: str,
    tenant_id: str,
    site_id: str,
    camera_id: str,
    stream_key: str,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """Issue a short-lived JWT granting read access to one MediaMTX path.

    Args:
        subject: Authenticated VMS principal subject.
        tenant_id: Authorized camera tenant.
        site_id: Authorized camera site.
        camera_id: Authorized camera identity.
        stream_key: Exact MediaMTX live path allowed by the grant.
        now: Optional deterministic issuance time for tests.

    Returns:
        Encoded RS256 JWT and its expiry timestamp.

    Raises:
        LiveAccessError: If the dedicated signing key is not configured safely.
    """
    _verification_keyring()
    key, kid = _signing_key()
    issued_at = now or datetime.now(timezone.utc)
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=timezone.utc)
    expires_at = issued_at + timedelta(seconds=settings.live_view_token_ttl_seconds)
    claims = {
        "iss": LIVE_ACCESS_ISSUER,
        "aud": LIVE_ACCESS_AUDIENCE,
        "sub": subject,
        "iat": issued_at,
        "exp": expires_at,
        "jti": secrets.token_urlsafe(16),
        "tenant_id": tenant_id,
        "site_id": site_id,
        "camera_id": camera_id,
        "path": stream_key,
        "mediamtx_permissions": [{"action": "read", "path": stream_key}],
    }
    return (
        jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid}),
        expires_at,
    )
