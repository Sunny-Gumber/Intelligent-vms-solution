"""Static invariants of the shipped deployment inputs (ADR-0015, R2/R3/R5/R8).

EVIDENCE CLASS: configuration inspection. Proves what the files say, not
that the pinned gateway accepts or enforces them -- that needs
`mediamtx --validate-conf` and tests/gateway/test_mediamtx_gateway.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "deploy" / "docker"
LEGACY_AUTH_KEYS = {"readUser", "readPass", "publishUser", "publishPass", "readIPs",
                    "publishIPs", "authMethods", "externalAuthenticationURL"}
PINNED_IMAGE = re.compile(r"^bluenviron/mediamtx:\d+\.\d+\.\d+(@sha256:[0-9a-f]{64})?$")


@pytest.fixture(scope="module")
def gateway_conf() -> dict:
    return yaml.safe_load((DOCKER / "mediamtx.yml").read_text())


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((DOCKER / "docker-compose.yml").read_text())


def _keys(node) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(str(key))
            found |= _keys(value)
    elif isinstance(node, list):
        for item in node:
            found |= _keys(item)
    return found


def test_gateway_delegates_every_action_to_the_hook(gateway_conf):
    assert gateway_conf["authMethod"] == "http"
    assert gateway_conf["authHTTPExclude"] == [], "an omitted list is version-dependent"
    assert "authInternalUsers" not in gateway_conf


def test_gateway_file_holds_no_hook_secret_and_fails_closed(gateway_conf):
    address = gateway_conf["authHTTPAddress"]
    assert "@" not in address, "the hook secret must be injected at runtime"
    assert address.startswith("http://127.0.0.1:9/")


def test_gateway_has_no_legacy_credential_keys(gateway_conf):
    assert not (_keys(gateway_conf) & LEGACY_AUTH_KEYS)


def test_gateway_admin_surfaces(gateway_conf):
    assert gateway_conf["apiAddress"] == ":9997", "must be reachable by the peer container"
    assert gateway_conf["metricsAddress"].startswith("127.0.0.1:")
    assert gateway_conf["pprof"] is False
    assert gateway_conf["playback"] is False
    assert gateway_conf["rtspAuthMethods"] == ["basic"]
    assert gateway_conf["rtmp"] is False and gateway_conf["srt"] is False
    assert gateway_conf["moq"] is False


def test_gateway_image_is_pinned(compose):
    image = compose["services"]["mediamtx"]["image"]
    assert PINNED_IMAGE.match(image), image


def test_admin_ports_are_never_published(compose):
    for name, service in compose["services"].items():
        for mapping in service.get("ports", []):
            host_side = str(mapping).split("/")[0]
            assert not host_side.endswith(("9997", "9998", "9999")), (name, mapping)


def test_hook_callback_matches_listener_route_and_trust(compose):
    gateway = compose["services"]["mediamtx"]
    api = compose["services"]["control-api"]
    address = gateway["environment"]["MTX_AUTHHTTPADDRESS"]
    assert re.fullmatch(r"http://mediamtx:\$\{VMS_MEDIA_AUTH_HOOK_SECRET:\?[^}]+\}"
                        r"@control-api-internal:8080/internal/media/auth", address)
    assert "control-api-internal" in api["networks"]["media-admin"]["aliases"]
    dockerfile = (DOCKER / "Dockerfile.control-api").read_text()
    assert "VMS_API_PORT=8080" in dockerfile and "EXPOSE 8080" in dockerfile
    gateway_ip = gateway["networks"]["media-admin"]["ipv4_address"]
    assert api["environment"]["VMS_MEDIA_AUTH_ALLOWED_CALLERS"] == f"{gateway_ip}/32"
    assert gateway["environment"]["MTX_APIADDRESS"] == f"{gateway_ip}:9997"
    assert api["environment"]["VMS_MEDIA_GATEWAY_URL"] == "http://mediamtx-admin:9997"
    assert "control-api-internal" in api["environment"]["VMS_TRUSTED_HOSTS"]


def test_admin_network_is_internal(compose):
    assert compose["networks"]["media-admin"]["internal"] is True


def test_hook_route_is_registered_where_the_gateway_calls_it():
    from app.api.internal.media_auth import router
    assert "/internal/media/auth" in {route.path for route in router.routes}


def test_native_toml_maps_production_and_media_settings(monkeypatch, tmp_path):
    config_file = tmp_path / "vms.toml"
    config_file.write_text(
        '[vms]\n'
        'environment = "production"\n'
        'secret_key = "native-test-secret-key-with-more-than-32-characters"\n'
        'database_url = "postgresql+psycopg://localhost/vms"\n'
        'trusted_hosts = ["localhost", "127.0.0.1"]\n'
        '[media]\n'
        'gateway_url = "http://127.0.0.1:9997"\n'
        'gateway_user = "vms-control"\n'
        'gateway_password = "native-gateway-password-1234"\n'
        'auth_hook_user = "mediamtx"\n'
        'auth_hook_secret = "native-hook-secret-long-enough-1234567890"\n'
        'auth_allowed_callers = ["127.0.0.1/32"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("VMS_CONFIG_FILE", str(config_file))

    from app.config import Settings, _file_defaults

    settings = Settings(**_file_defaults())
    assert settings.environment == "production"
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.media_auth_problems() == []


def test_ubuntu_installer_configures_media_auth_before_services():
    installer = (ROOT / "deploy" / "ubuntu" / "install.sh").read_text()
    api_service = (ROOT / "deploy" / "ubuntu" / "intelligent-vms-api.service").read_text()
    gateway_service = (ROOT / "deploy" / "ubuntu" / "intelligent-vms-mediamtx.service").read_text()

    assert "VMS_MEDIA_AUTH_HOOK_SECRET=${HOOK_SECRET}" in installer
    assert "VMS_MEDIA_AUTH_ALLOWED_CALLERS=127.0.0.1/32" in installer
    assert "MTX_AUTHHTTPADDRESS=http://mediamtx:${HOOK_SECRET}@" in installer
    assert "media_auth_problems()" in installer
    assert "EnvironmentFile=/etc/intelligent-vms/vms.env" in api_service
    assert "EnvironmentFile=/etc/intelligent-vms/vms.env" in gateway_service


def test_windows_installer_preflights_and_protects_media_auth():
    installer = (ROOT / "installers" / "windows" / "install.ps1").read_text()

    assert 'auth_hook_secret = "$hookSecret"' in installer
    assert 'auth_allowed_callers = ["127.0.0.1/32"]' in installer
    assert "media_auth_problems()" in installer
    assert "Set-PrivateDirectoryAcl \"$DataPath\\config\"" in installer
    assert "MediaMTX 1.21.2" in installer
