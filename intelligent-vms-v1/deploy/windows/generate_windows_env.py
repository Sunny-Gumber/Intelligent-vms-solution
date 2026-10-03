#!/usr/bin/env python3
"""Generate the private Windows small-site field-test configuration."""
from __future__ import annotations

import argparse
import base64
import secrets
from pathlib import Path, PureWindowsPath

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def _secret() -> str:
    """Return a high-entropy hexadecimal secret."""
    return secrets.token_hex(32)


def _private_key_b64() -> str:
    """Return a base64 PKCS8 RSA private key for live-view token signing."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return base64.b64encode(pem).decode("ascii")


def _windows_forward(path: Path) -> str:
    """Represent a Windows path with MediaMTX/Python-friendly forward slashes."""
    return PureWindowsPath(str(path)).as_posix()


def generate(
    output: Path,
    *,
    recording_dir: Path,
    app_root: Path,
    venv_python: Path,
    postgres_password: str,
    postgres_service: str,
    camera_cidrs: list[str],
    force: bool = False,
) -> Path:
    """Create one protected Windows field-test environment file."""
    if output.exists() and not force:
        raise FileExistsError(f"{output} already exists; refusing to rotate secrets")
    recording_text = str(recording_dir)
    if recording_text.startswith("\\"):
        raise ValueError("UNC/network recording storage is not supported by this field-test baseline")
    if not PureWindowsPath(recording_text).is_absolute():
        raise ValueError("recording directory must be an absolute Windows path")

    cidrs = list(camera_cidrs) or ["192.168.0.0/16", "127.0.0.1/32"]
    if "127.0.0.1/32" not in cidrs:
        cidrs.append("127.0.0.1/32")

    hook = (
        f'"{_windows_forward(venv_python)}" '
        f'"{_windows_forward(app_root / "deploy/windows/recording_hook.py")}" '
        f'--env-file "{_windows_forward(output)}"'
    )
    record_path = _windows_forward(recording_dir) + "/%path/%Y/%m/%d/%H/%s-%f"
    values = {
        "DEPLOYMENT_PROFILE": "windows-small-site",
        "WINDOWS_RECORDING_ROOT": _windows_forward(recording_dir),
        "WINDOWS_APP_ROOT": _windows_forward(app_root),
        "WINDOWS_POSTGRES_SERVICE": postgres_service,
        "WINDOWS_POSTGRES_PASSWORD": postgres_password,
        "DATABASE_URL": f"postgresql+asyncpg://vms:{postgres_password}@127.0.0.1:5432/vms",
        "VMS_SECRET_KEY": _secret(),
        "AUTO_CREATE_SCHEMA": "false",
        "WEB_STATIC_DIR": _windows_forward(app_root / "web"),
        "MEDIAMTX_API_URL": "http://127.0.0.1:9997",
        "MEDIAMTX_WEBRTC_PUBLIC_BASE": "http://127.0.0.1:8889",
        "MEDIAMTX_HLS_PUBLIC_BASE": "http://127.0.0.1:8888",
        "MEDIAMTX_PLAYBACK_INTERNAL_URL": "http://127.0.0.1:9996",
        "MEDIAMTX_METRICS_URL": "http://127.0.0.1:9998/metrics",
        "LIVE_VIEW_TOKEN_PRIVATE_KEY_B64": _private_key_b64(),
        "LIVE_VIEW_TOKEN_VERIFICATION_PUBLIC_KEYS_B64_JSON": "[]",
        "AUTH_DISABLED": "false",
        "AUTH_REQUIRE_OIDC": "false",
        "AUTH_HS256_SECRET": _secret(),
        "AUTH_BROWSER_SESSION_ENABLED": "true",
        "AUTH_BROWSER_SESSION_COOKIE_SECURE": "false",
        "AUTH_BROWSER_SESSION_MAX_AGE_SECONDS": "28800",
        "AUTH_AUDIENCE": "intelligent-vms",
        "CORS_ALLOWED_ORIGINS": "http://127.0.0.1:8000,http://localhost:8000",
        "CORS_ALLOWED_HEADERS": "Authorization,Content-Type,Range,X-VMS-CSRF",
        "KAFKA_BOOTSTRAP_SERVERS": "",
        "OUTBOX_ENABLED": "false",
        "EVENT_PIPELINE_ENABLED": "false",
        "EVENT_HISTORY_ENABLED": "false",
        "ALARM_PROCESSING_ENABLED": "false",
        "AI_UI_ENABLED": "false",
        "RECORDING_METADATA_EVENTS_ENABLED": "false",
        "CLICKHOUSE_URL": "",
        "PLACEMENT_EXECUTION_ENABLED": "false",
        "RECORDING_HOOK_TOKEN": _secret(),
        "RECORDING_HOOK_CALLBACK_URL": "http://127.0.0.1:8000/internal/v1/recording/segments/complete",
        "RECORDING_HOOK_COMMAND": hook,
        "RECORDING_PATH_TEMPLATE": record_path,
        "REGIONAL_SPOOL_TOKEN": _secret(),
        "NODE_AGENT_TOKEN": _secret(),
        "ONVIF_SITE_ALLOWED_CIDRS_JSON": '{"field-test/site-01":['
        + ",".join(f'"{item}"' for item in cidrs)
        + "]}",
        "ONVIF_DISCOVERY_LOCAL_SITES": "field-test/site-01",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8")
    print(f"windows_field_test_env_ok file={output} recordings={recording_dir}")
    print("secret values were generated but intentionally not printed")
    return output


def main() -> None:
    """Parse generation arguments and write one Windows field-test environment."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--recording-dir", required=True, type=Path)
    parser.add_argument("--app-root", required=True, type=Path)
    parser.add_argument("--venv-python", required=True, type=Path)
    parser.add_argument("--postgres-password", required=True)
    parser.add_argument("--postgres-service", required=True)
    parser.add_argument("--camera-cidr", action="append", default=[])
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    generate(
        args.output,
        recording_dir=args.recording_dir,
        app_root=args.app_root,
        venv_python=args.venv_python,
        postgres_password=args.postgres_password,
        postgres_service=args.postgres_service,
        camera_cidrs=args.camera_cidr,
        force=args.force,
    )


if __name__ == "__main__":
    main()
