import asyncio
from pathlib import Path

import pytest
import yaml

from app.core.config import settings
from app.core.security import encrypt_secret
from app.core.security_posture import validate_security_posture
from app.services.mediamtx import MediaMTXClient
from deploy.windows.generate_windows_env import generate

ROOT = Path(__file__).parents[1]
INSTALL = (ROOT / "deploy/windows/Install-WindowsFieldTest.ps1").read_text(encoding="utf-8")
LIFECYCLE = (ROOT / "deploy/windows/Vms-Windows.ps1").read_text(encoding="utf-8")
DIAGNOSTICS = (ROOT / "deploy/windows/Collect-WindowsDiagnostics.ps1").read_text(encoding="utf-8")
SERVICE_HOST = (ROOT / "deploy/windows/service_host.py").read_text(encoding="utf-8")
SERVICE_MANAGER = (ROOT / "deploy/windows/service_manager.py").read_text(encoding="utf-8")
WINDOWS_MEDIA = yaml.safe_load((ROOT / "deploy/windows/mediamtx.windows.yml").read_text(encoding="utf-8"))
WEB = (ROOT / "web/index.html").read_text(encoding="utf-8")
MAIN = (ROOT / "services/control-api/app/main.py").read_text(encoding="utf-8")
RECORDINGS = (ROOT / "services/control-api/app/routers/recordings.py").read_text(encoding="utf-8")
EVENTS = (ROOT / "services/control-api/app/routers/events.py").read_text(encoding="utf-8")
ALARMS = (ROOT / "services/control-api/app/routers/alarms.py").read_text(encoding="utf-8")
ADR = (ROOT / "docs/architecture/ADR_WINDOWS_NATIVE_FIELD_TEST.md").read_text(encoding="utf-8")
OPS = (ROOT / "docs/operations/WINDOWS_FIELD_TEST.md").read_text(encoding="utf-8")
SMOKE = (ROOT / "docs/testing/WINDOWS_FIELD_TEST_SMOKE.md").read_text(encoding="utf-8")
WORKFLOW = (ROOT.parent / ".github/workflows/intelligent-vms-windows-field-test.yml").read_text(encoding="utf-8")


def test_windows_generator_rejects_unc_recording_storage(tmp_path):
    with pytest.raises(ValueError, match="UNC"):
        generate(
            tmp_path / "vms.env",
            recording_dir=Path(r"\\server\recordings"),
            app_root=Path(r"C:\ProgramData\IntelligentVMS\app"),
            venv_python=Path(r"C:\ProgramData\IntelligentVMS\runtime\venv\Scripts\python.exe"),
            postgres_password="a" * 64,
            postgres_service="postgresql-x64-17",
            camera_cidrs=["127.0.0.1/32"],
        )


def test_windows_generator_creates_explicit_small_site_profile(tmp_path, capsys):
    output = tmp_path / "vms.env"
    generate(
        output,
        recording_dir=Path("D:/VMS/Recordings"),
        app_root=Path("C:/ProgramData/IntelligentVMS/app"),
        venv_python=Path("C:/ProgramData/IntelligentVMS/runtime/venv/Scripts/python.exe"),
        postgres_password="b" * 64,
        postgres_service="postgresql-x64-17",
        camera_cidrs=["10.20.30.0/24"],
    )
    body = output.read_text(encoding="utf-8")
    stdout = capsys.readouterr().out
    assert "DEPLOYMENT_PROFILE=windows-small-site" in body
    assert "KAFKA_BOOTSTRAP_SERVERS=\n" in body
    assert "OUTBOX_ENABLED=false" in body
    assert "EVENT_PIPELINE_ENABLED=false" in body
    assert "EVENT_HISTORY_ENABLED=false" in body
    assert "RECORDING_METADATA_EVENTS_ENABLED=false" in body
    assert "PLACEMENT_EXECUTION_ENABLED=false" in body
    assert "RECORDING_PATH_TEMPLATE=D:/VMS/Recordings/%path/%Y/%m/%d/%H/%s" in body
    assert 'RECORDING_HOOK_COMMAND="C:/ProgramData/IntelligentVMS/runtime/venv/Scripts/python.exe"' in body
    assert "bbbbbbbb" not in stdout
    assert "secret values were generated but intentionally not printed" in stdout


def test_windows_recording_hook_override_keeps_shared_mediamtx_contract(monkeypatch):
    captured = {}

    async def fake_upsert(stream_key, payload):
        captured["stream_key"] = stream_key
        captured["payload"] = payload

    monkeypatch.setattr(settings, "recording_hook_command", "C:/VMS/python.exe C:/VMS/recording_hook.py")
    client = MediaMTXClient("http://media.invalid")
    monkeypatch.setattr(client, "_upsert", fake_upsert)
    asyncio.run(
        client.add_or_replace_recording_path(
            "cam-record",
            "rtsp://10.0.0.2/main",
            retention_days=7,
            part_duration_ms=1000,
            segment_duration_seconds=900,
            max_part_size_mb=50,
        )
    )
    payload = captured["payload"]
    assert payload["runOnRecordSegmentComplete"] == "C:/VMS/python.exe C:/VMS/recording_hook.py"
    assert payload["record"] is True
    assert payload["sourceOnDemand"] is False
    assert payload["recordPath"] == settings.recording_path_template


def test_small_site_does_not_enqueue_undeliverable_recording_metadata():
    block = RECORDINGS.split("payload = {", 1)[1].split("@internal_router", 1)[0]
    assert "if settings.recording_metadata_events_enabled:" in block
    assert block.index("if settings.recording_metadata_events_enabled:") < block.index("enqueue_message_once(")
    assert block.index("record_segment_completion(") > block.index("enqueue_message_once(")
    assert '"metadata_event_enqueued"' in block


def test_small_site_event_pipeline_is_explicitly_unavailable():
    assert EVENTS.count("Event pipeline unavailable in deployment profile") >= 2
    assert "Event history unavailable in deployment profile" in EVENTS
    assert "Alarm processing unavailable in deployment profile" in ALARMS
    assert "dependencies=[Depends(_require_alarm_processing)]" in ALARMS
    assert "event_history" in WEB and "alarm_processing" in WEB and "ai_ui" in WEB
    assert "/system/capabilities" in WEB


def test_control_api_can_serve_shared_web_assets_without_second_backend():
    assert "StaticFiles" in MAIN
    assert 'app.mount("/", StaticFiles(directory=static_root, html=True), name="web")' in MAIN
    assert "web_static_dir" in (ROOT / "services/control-api/app/core/config.py").read_text(encoding="utf-8")


def test_windows_service_model_is_native_and_dependency_ordered():
    assert "IntelligentVMSControl" in SERVICE_HOST
    assert "IntelligentVMSMedia" in SERVICE_HOST
    assert "win32serviceutil.InstallService" in SERVICE_MANAGER
    assert "win32serviceutil.LocatePythonServiceExe()" in SERVICE_MANAGER
    assert "sitePackages \"win32\\pythonservice.exe\"" not in INSTALL
    assert 'serviceDeps=[postgres_service]' in SERVICE_MANAGER
    assert 'serviceDeps=["IntelligentVMSControl"]' in SERVICE_MANAGER
    assert "SERVICE_AUTO_START" in SERVICE_MANAGER
    assert "Docker" not in SERVICE_MANAGER and "WSL" not in SERVICE_MANAGER


def test_windows_env_importer_preserves_empty_and_embedded_equals_values():
    """Parse env assignments at the first separator without dropping empty values."""
    importer = INSTALL.split("function Import-VmsEnv", 1)[1].split("function Invoke-Checked", 1)[0]
    assert '.Split("=", 2)' not in importer
    assert '$separator = $line.IndexOf("=")' in importer
    assert '$name = $line.Substring(0, $separator).Trim()' in importer
    assert '$value = $line.Substring($separator + 1).Trim()' in importer
    assert '$value.Length -ge 2' in importer
    assert "SetEnvironmentVariable($name, $value" in importer
    # The reduced profile intentionally emits an empty assignment that the importer must preserve.
    assert "KAFKA_BOOTSTRAP_SERVERS=\\n" in (
        ROOT / "deploy/windows/generate_windows_env.py"
    ).read_text(encoding="utf-8") or "KAFKA_BOOTSTRAP_SERVERS=" in (
        ROOT / "deploy/windows/generate_windows_env.py"
    ).read_text(encoding="utf-8")


def test_windows_installer_is_non_docker_pins_media_and_protects_paths():
    lower = INSTALL.lower()
    assert "docker.exe" not in lower and "docker compose" not in lower
    assert "wsl.exe" not in lower and "wsl --" not in lower
    assert "faa97974861eb75a68b5aa326c78e7e7a6f670b5ef191bace78e715130381f23" in INSTALL
    assert "Get-FileHash -Algorithm SHA256" in INSTALL
    assert "UNC/network recording storage is not supported" in INSTALL
    assert "icacls.exe" in INSTALL
    assert '"-m","alembic"' in INSTALL
    assert '([string]$roleExists).Trim()' in INSTALL
    assert '([string]$dbExists).Trim()' in INSTALL


def test_uninstall_and_purge_are_non_destructive_by_default():
    uninstall = LIFECYCLE.split('"Uninstall" {', 1)[1].split('"PurgeDatabase" {', 1)[0]
    purge = LIFECYCLE.split('"PurgeDatabase" {', 1)[1]
    assert "Remove-Item" not in uninstall
    assert "dropdb.exe" not in uninstall
    assert "DELETE_WINDOWS_FIELD_TEST_DATABASE" in purge
    assert "Remove-Item" not in purge
    assert "recordings and protected configuration remain untouched" in purge


def test_windows_diagnostics_are_bounded_and_redact_credentials():
    assert "-Tail 500" in DIAGNOSTICS
    assert "[REDACTED]" in DIAGNOSTICS
    assert "rtsp|rtsps|http|https" in DIAGNOSTICS
    assert "config-presence.json" in DIAGNOSTICS
    assert "Get-NetFirewallRule" in DIAGNOSTICS
    assert "VMS_SECRET_KEY" in DIAGNOSTICS


def test_windows_media_is_loopback_only_and_firewall_not_disabled():
    for key in ("apiAddress", "metricsAddress", "playbackAddress", "hlsAddress", "webrtcAddress", "rtspAddress"):
        assert str(WINDOWS_MEDIA[key]).startswith("127.0.0.1:")
    assert str(WINDOWS_MEDIA["webrtcLocalUDPAddress"]).startswith("127.0.0.1:")
    assert "Set-NetFirewallProfile" not in INSTALL
    assert "New-NetFirewallRule" not in INSTALL


def test_windows_ci_matrix_is_honest_about_available_hosted_os():
    assert "windows-2022" in WORKFLOW
    assert "windows-2025" in WORKFLOW
    assert "windows-11" not in WORKFLOW
    assert "windows-10" not in WORKFLOW
    for marker in ("Windows 11 64-bit", "Windows 10 64-bit", "External Qualification Pending"):
        assert marker in OPS


def test_windows_profile_does_not_claim_desktop_client_or_production_qualification():
    assert "does not start the native Windows desktop client" in OPS
    assert "Release Candidate / External Qualification Pending" in OPS
    assert "Production Qualified" in SMOKE


def test_architecture_keeps_enterprise_distributed_profile():
    assert "enterprise-distributed" in ADR
    assert "Redpanda/Kafka" in ADR
    assert "ClickHouse" in ADR
    assert "no SQLite shortcut" in ADR


def test_windows_small_site_profile_fails_closed_if_enterprise_services_are_enabled(monkeypatch):
    monkeypatch.setattr(settings, "deployment_profile", "windows-small-site")
    monkeypatch.setattr(settings, "kafka_bootstrap_servers", "")
    monkeypatch.setattr(settings, "outbox_enabled", False)
    monkeypatch.setattr(settings, "event_pipeline_enabled", False)
    monkeypatch.setattr(settings, "event_history_enabled", False)
    monkeypatch.setattr(settings, "alarm_processing_enabled", False)
    monkeypatch.setattr(settings, "ai_ui_enabled", False)
    monkeypatch.setattr(settings, "recording_metadata_events_enabled", False)
    monkeypatch.setattr(settings, "placement_execution_enabled", False)
    monkeypatch.setattr(settings, "clickhouse_url", "")
    monkeypatch.setattr(settings, "auth_require_oidc", False)
    validate_security_posture()

    monkeypatch.setattr(settings, "event_history_enabled", True)
    with pytest.raises(RuntimeError, match="EVENT_HISTORY_ENABLED"):
        validate_security_posture()


def test_windows_ci_uses_test_only_secret_and_production_default_fails_closed(monkeypatch):
    """Keep the Windows CI key explicit while production remains fail-closed."""
    assert "VMS_SECRET_KEY: windows-ci-test-key-not-for-production" in WORKFLOW
    config_source = (ROOT / "services/control-api/app/core/config.py").read_text(encoding="utf-8")
    assert 'vms_secret_key: str = ""' in config_source
    monkeypatch.setattr(settings, "vms_secret_key", "")
    with pytest.raises(RuntimeError, match="VMS_SECRET_KEY must be configured"):
        encrypt_secret("synthetic-camera-password")
