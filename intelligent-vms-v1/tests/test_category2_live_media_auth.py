import asyncio
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jwt.algorithms import RSAAlgorithm

from app.core.auth import Principal
from app.core.config import settings
from app.models.entities import CameraEntity
from app.routers import live_media
from app.services.live_access import (
    LIVE_ACCESS_AUDIENCE,
    LIVE_ACCESS_ISSUER,
    LiveAccessError,
    issue_live_access_token,
    live_access_jwks,
)

FIXED_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
TEST_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
TEST_PRIVATE_PEM = TEST_PRIVATE_KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
TEST_PRIVATE_B64 = base64.b64encode(TEST_PRIVATE_PEM).decode("ascii")
NEXT_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
UNKNOWN_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_key_b64(key):
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(pem).decode("ascii")


def principal(*, tenant_id="tenant-a", sites=("site-a",)):
    return Principal(
        subject="viewer-1",
        roles=frozenset({"viewer"}),
        tenant_id=tenant_id,
        site_ids=frozenset(sites),
    )


def camera():
    return CameraEntity(
        id="camera-1",
        tenant_id="tenant-a",
        site_id="site-a",
        name="Gate",
        host="192.168.1.20",
        rtsp_port=554,
        main_path="/main",
        sub_path="/sub",
        stream_key="site-a-gate-abcd1234",
        media_node_id="media-local-01",
        enabled=True,
        desired_state="provisioned",
        created_at=FIXED_NOW,
    )


class Session:
    def __init__(self, cam):
        self.cam = cam

    async def get(self, model, key):
        if model is CameraEntity and self.cam.id == key:
            return self.cam
        return None


@pytest.fixture(autouse=True)
def live_key(monkeypatch):
    monkeypatch.setattr(settings, "live_view_token_private_key_b64", TEST_PRIVATE_B64)
    monkeypatch.setattr(settings, "live_view_token_verification_public_keys_b64_json", "[]")
    monkeypatch.setattr(settings, "live_view_token_ttl_seconds", 60)


def decode_grant(token):
    header = jwt.get_unverified_header(token)
    jwk = next(key for key in live_access_jwks()["keys"] if key["kid"] == header["kid"])
    return jwt.decode(
        token,
        RSAAlgorithm.from_jwk(jwk),
        algorithms=["RS256"],
        audience=LIVE_ACCESS_AUDIENCE,
        issuer=LIVE_ACCESS_ISSUER,
        options={"verify_exp": False, "verify_iat": False},
    )


def test_live_grant_is_short_lived_and_exact_path_bound():
    token, expires_at = issue_live_access_token(
        subject="viewer-1",
        tenant_id="tenant-a",
        site_id="site-a",
        camera_id="camera-1",
        stream_key="site-a-gate-abcd1234",
        now=FIXED_NOW,
    )

    claims = decode_grant(token)
    header = jwt.get_unverified_header(token)
    jwk = live_access_jwks()["keys"][0]

    assert expires_at == FIXED_NOW + timedelta(seconds=60)
    assert claims["camera_id"] == "camera-1"
    assert claims["tenant_id"] == "tenant-a"
    assert claims["site_id"] == "site-a"
    assert claims["path"] == "site-a-gate-abcd1234"
    assert claims["mediamtx_permissions"] == [
        {"action": "read", "path": "site-a-gate-abcd1234"}
    ]
    assert header["alg"] == "RS256"
    assert header["kid"] == jwk["kid"]
    assert jwk["kty"] == "RSA"
    assert "d" not in jwk


def test_live_grant_has_no_publish_or_cross_path_permission():
    token, _ = issue_live_access_token(
        subject="viewer-1",
        tenant_id="tenant-a",
        site_id="site-a",
        camera_id="camera-1",
        stream_key="site-a-gate-abcd1234",
        now=FIXED_NOW,
    )

    permissions = decode_grant(token)["mediamtx_permissions"]

    assert {"action": "read", "path": "other-camera-path"} not in permissions
    assert not any(permission["action"] == "publish" for permission in permissions)


def test_live_grant_rejects_missing_malformed_or_weak_private_key(monkeypatch):
    for encoded in ("", base64.b64encode(b"not-a-key").decode("ascii")):
        monkeypatch.setattr(settings, "live_view_token_private_key_b64", encoded)
        with pytest.raises(LiveAccessError):
            issue_live_access_token(
                subject="viewer-1",
                tenant_id="tenant-a",
                site_id="site-a",
                camera_id="camera-1",
                stream_key="site-a-gate-abcd1234",
                now=FIXED_NOW,
            )

    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    weak_pem = weak.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    monkeypatch.setattr(
        settings,
        "live_view_token_private_key_b64",
        base64.b64encode(weak_pem).decode("ascii"),
    )
    with pytest.raises(LiveAccessError):
        live_access_jwks()


def test_out_of_scope_viewer_cannot_mint_live_grant():
    cam = camera()
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            live_media.create_live_access(
                cam.id,
                "live",
                Session(cam),
                principal(tenant_id="tenant-b", sites=("site-b",)),
            )
        )

    assert error.value.status_code == 404


def test_public_jwks_contains_no_private_key_material():
    response = asyncio.run(live_media.media_jwks())
    key = response["keys"][0]

    assert key["alg"] == "RS256"
    assert key["use"] == "sig"
    assert "n" in key and "e" in key
    assert "d" not in key
    assert "p" not in key
    assert "q" not in key


def test_token_claims_do_not_contain_camera_credentials():
    token, _ = issue_live_access_token(
        subject="viewer-1",
        tenant_id="tenant-a",
        site_id="site-a",
        camera_id="camera-1",
        stream_key="site-a-gate-abcd1234",
        now=FIXED_NOW,
    )

    serialized = repr(decode_grant(token))
    assert "username" not in serialized
    assert "password" not in serialized
    assert "rtsp://" not in serialized


def test_authorized_viewer_can_mint_credential_free_live_access(monkeypatch):
    cam = camera()
    monkeypatch.setattr(settings, "placement_execution_enabled", False)
    monkeypatch.setattr(settings, "mediamtx_webrtc_public_base", "https://media.example/webrtc")
    monkeypatch.setattr(settings, "mediamtx_hls_public_base", "https://media.example/hls")

    result = asyncio.run(
        live_media.create_live_access(
            cam.id,
            "live",
            Session(cam),
            principal(),
        )
    )

    assert result.camera_id == cam.id
    assert result.path == cam.stream_key
    assert result.webrtc_url == "https://media.example/webrtc/site-a-gate-abcd1234"
    assert result.hls_url == "https://media.example/hls/site-a-gate-abcd1234"
    assert "token=" not in result.webrtc_url
    assert "token=" not in result.hls_url
    assert result.access_token not in result.webrtc_url
    assert result.access_token not in result.hls_url


def test_mediamtx_config_uses_cached_jwks_and_no_anonymous_live_read():
    root = Path(__file__).parents[1]
    config = (root / "infra" / "mediamtx" / "mediamtx.yml").read_text(encoding="utf-8")

    assert "authMethod: jwt" in config
    assert "authJWTJWKS: http://control-api:8000/internal/v1/media/jwks" in config
    assert "authJWTIssuer: intelligent-vms-control" in config
    assert "authJWTAudience: intelligent-vms-live" in config
    assert "authHTTPAddress:" not in config
    assert "- action: read" not in config
    assert "- action: publish" not in config


def test_development_client_keeps_live_grant_out_of_urls_and_cross_origin_teardown():
    root = Path(__file__).parents[1]
    web = (root / "web" / "index.html").read_text(encoding="utf-8")

    assert "Authorization:'Bearer '+access.access_token" in web
    assert "?token=" not in web
    assert "candidate.origin!==origin" in web
    assert "cross-origin WHEP session rejected" in web


def test_rotation_prepublishes_next_key_without_changing_active_signer(monkeypatch):
    monkeypatch.setattr(
        settings,
        "live_view_token_verification_public_keys_b64_json",
        f'["{public_key_b64(NEXT_PRIVATE_KEY)}"]',
    )
    token, _ = issue_live_access_token(
        subject="viewer-1",
        tenant_id="tenant-a",
        site_id="site-a",
        camera_id="camera-1",
        stream_key="site-a-gate-abcd1234",
        now=FIXED_NOW,
    )
    jwks = live_access_jwks()["keys"]
    assert jwt.get_unverified_header(token)["kid"] == jwks[0]["kid"]
    assert len(jwks) == 2
    assert len({key["kid"] for key in jwks}) == 2


def test_rotation_switches_signer_while_retaining_old_verification_key(monkeypatch):
    old_token, _ = issue_live_access_token(
        subject="viewer-1",
        tenant_id="tenant-a",
        site_id="site-a",
        camera_id="camera-1",
        stream_key="site-a-gate-abcd1234",
        now=FIXED_NOW,
    )
    next_pem = NEXT_PRIVATE_KEY.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    monkeypatch.setattr(
        settings,
        "live_view_token_private_key_b64",
        base64.b64encode(next_pem).decode("ascii"),
    )
    monkeypatch.setattr(
        settings,
        "live_view_token_verification_public_keys_b64_json",
        f'["{public_key_b64(TEST_PRIVATE_KEY)}"]',
    )
    new_token, _ = issue_live_access_token(
        subject="viewer-1",
        tenant_id="tenant-a",
        site_id="site-a",
        camera_id="camera-1",
        stream_key="site-a-gate-abcd1234",
        now=FIXED_NOW,
    )
    assert (
        jwt.get_unverified_header(old_token)["kid"]
        != jwt.get_unverified_header(new_token)["kid"]
    )
    assert decode_grant(old_token)["sub"] == "viewer-1"
    assert decode_grant(new_token)["sub"] == "viewer-1"


def test_retired_and_unknown_kids_are_not_published(monkeypatch):
    old_token, _ = issue_live_access_token(
        subject="viewer-1",
        tenant_id="tenant-a",
        site_id="site-a",
        camera_id="camera-1",
        stream_key="site-a-gate-abcd1234",
        now=FIXED_NOW,
    )
    old_kid = jwt.get_unverified_header(old_token)["kid"]
    next_pem = NEXT_PRIVATE_KEY.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    monkeypatch.setattr(
        settings,
        "live_view_token_private_key_b64",
        base64.b64encode(next_pem).decode("ascii"),
    )
    published_kids = {key["kid"] for key in live_access_jwks()["keys"]}
    unknown_token = jwt.encode(
        {"sub": "viewer-1"},
        UNKNOWN_PRIVATE_KEY,
        algorithm="RS256",
        headers={"kid": "unknown-kid"},
    )
    assert old_kid not in published_kids
    assert jwt.get_unverified_header(unknown_token)["kid"] not in published_kids


def test_verification_keyring_fails_closed_on_invalid_private_duplicate_or_excess(
    monkeypatch,
):
    private_as_public = base64.b64encode(TEST_PRIVATE_PEM).decode("ascii")
    cases = [
        "not-json",
        '{"not":"a-list"}',
        f'["{private_as_public}"]',
        f'["{public_key_b64(TEST_PRIVATE_KEY)}"]',
    ]
    excess = [
        public_key_b64(rsa.generate_private_key(public_exponent=65537, key_size=2048))
        for _ in range(5)
    ]
    cases.append("[" + ",".join(f'"{key}"' for key in excess) + "]")
    for value in cases:
        monkeypatch.setattr(
            settings, "live_view_token_verification_public_keys_b64_json", value
        )
        with pytest.raises(LiveAccessError):
            live_access_jwks()


def test_invalid_overlap_keyring_blocks_token_issuance(monkeypatch):
    monkeypatch.setattr(
        settings, "live_view_token_verification_public_keys_b64_json", "not-json"
    )
    with pytest.raises(LiveAccessError):
        issue_live_access_token(
            subject="viewer-1",
            tenant_id="tenant-a",
            site_id="site-a",
            camera_id="camera-1",
            stream_key="site-a-gate-abcd1234",
            now=FIXED_NOW,
        )
