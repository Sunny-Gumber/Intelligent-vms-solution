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
    assert "EVENT_HISTORY_ENABLED=true" in body
    assert "EVENT_LOCAL_STORE_ENABLED=true" in body
    assert "EVENT_LOCAL_RETENTION_DAYS=7" in body
    assert "RECORDING_METADATA_EVENTS_ENABLED=false" in body
    assert "PLACEMENT_EXECUTION_ENABLED=false" in body
    assert "RECORDING_PATH_TEMPLATE=D:/VMS/Recordings/%path/%Y/%m/%d/%H/%s-%f" in body
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


def test_small_site_event_history_is_local_without_enterprise_pipeline():
    assert "Event pipeline unavailable in deployment profile" in EVENTS
    assert "Event history unavailable in deployment profile" in EVENTS
    assert "event_local_store_enabled" in EVENTS
    assert "EventHistoryEntity" in EVENTS
    assert '"/history"' in EVENTS
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
    assert 'Path(sys.prefix) / "pythonservice.exe"' in SERVICE_MANAGER
    assert "LocatePythonServiceExe()" not in SERVICE_MANAGER
    assert 'exeArgs="-service"' not in SERVICE_MANAGER
    assert "intelligent_vms_app.pth" in INSTALL
    assert "import deploy.windows.service_host" in INSTALL
    assert "windows_service_host_import_ok" in INSTALL
    assert "sys.base_prefix" in INSTALL
    assert "Python service runtime DLL missing" in INSTALL
    assert "python312._pth" in INSTALL
    assert "windows_service_runtime_import_ok" in INSTALL
    assert "servicemanager" in INSTALL
    assert "win32service" in INSTALL
    assert "win32event" in INSTALL
    assert "win32serviceutil" in INSTALL
    assert "pywintypes312.dll" in INSTALL
    assert "pythoncom312.dll" in INSTALL
    assert 'Protect-Directory $RuntimeRoot' in INSTALL
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
    # Empty reduced-profile assignments are exercised by
    # test_windows_generator_creates_explicit_small_site_profile; this test
    # protects the importer implementation that preserves empty/embedded "=" values.


def test_windows_installer_is_non_docker_pins_media_and_protects_paths():
    lower = INSTALL.lower()
    assert "docker.exe" not in lower and "docker compose" not in lower
    assert "wsl.exe" not in lower and "wsl --" not in lower
    assert "faa97974861eb75a68b5aa326c78e7e7a6f670b5ef191bace78e715130381f23" in INSTALL
    assert "function Get-Sha256Hex" in INSTALL
    assert "[Security.Cryptography.SHA256]::Create()" in INSTALL
    assert "$actual = Get-Sha256Hex $mediaZip" in INSTALL
    assert "UNC/network recording storage is not supported" in INSTALL
    assert "icacls.exe" in INSTALL
    assert '"-m","alembic"' in INSTALL
    assert '$roleFound=@($roleResult.Lines|ForEach-Object{$_.Trim()}) -contains "1"' in INSTALL
    assert '$databaseFound=@($dbResult.Lines|ForEach-Object{$_.Trim()}) -contains "1"' in INSTALL
    assert 'Invoke-Bounded (Join-Path $pgBin "psql.exe")' in INSTALL
    assert '([string]$roleExists).Trim()' not in INSTALL
    assert '([string]$dbExists).Trim()' not in INSTALL


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
    monkeypatch.setattr(settings, "event_history_enabled", True)
    monkeypatch.setattr(settings, "event_local_store_enabled", True)
    monkeypatch.setattr(settings, "alarm_processing_enabled", False)
    monkeypatch.setattr(settings, "ai_ui_enabled", False)
    monkeypatch.setattr(settings, "recording_metadata_events_enabled", False)
    monkeypatch.setattr(settings, "placement_execution_enabled", False)
    monkeypatch.setattr(settings, "clickhouse_url", "")
    monkeypatch.setattr(settings, "auth_require_oidc", False)
    validate_security_posture()

    monkeypatch.setattr(settings, "event_local_store_enabled", False)
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


def test_windows_failure_evidence_runs_before_cleanup_and_avoids_secrets():
    failure = WORKFLOW.index("Capture Windows service failure evidence")
    cleanup = WORKFLOW.index("Cleanup ephemeral hosted-runner state")
    assert failure < cleanup
    block = WORKFLOW[failure:cleanup]
    assert "if: failure()" in block
    for name in ("IntelligentVMSControl", "IntelligentVMSMedia"):
        assert name in block
    assert "sc.exe qc" in block and "sc.exe queryex" in block
    assert "CurrentControlSet" in block
    for field in ("ImagePath", "ObjectName", "DependOnService", "PythonClass", "PythonPath", "AppDirectory"):
        assert field in block
    assert "Get-WinEvent" in block
    for provider in ("Service Control Manager", "Python Service", "PythonService", "Application Error", "Windows Error Reporting"):
        assert provider in block
    for runtime_file in ("pythonservice.exe", "python312.dll", "python.exe", "pythonw.exe", "pyvenv.cfg"):
        assert runtime_file in block
    assert "pywintypes*.dll" in block and "pythoncom*.dll" in block
    assert "=== redacted product log tails ===" in block
    assert "Collect-WindowsDiagnostics.ps1" in block
    assert "-Tail 100" in block
    assert "vms.env" not in block
    assert "VMS_SECRET_KEY" not in block


def test_windows_pywin32_registration_and_event_metadata_diagnostics_contract():
    assert "deploy.windows.service_host.ControlService" in SERVICE_MANAGER
    assert "deploy.windows.service_host.MediaService" in SERVICE_MANAGER
    failure = WORKFLOW.index("Capture Windows service failure evidence")
    cleanup = WORKFLOW.index("Cleanup ephemeral hosted-runner state")
    block = WORKFLOW[failure:cleanup]
    assert '$pythonClassKey = "$key\\\\PythonClass"' in block
    assert '.GetValue("")' in block
    assert "PythonClass(default)=" in block
    assert '$_.ProviderName -eq "Python Service" -and $_.Id -eq 14' in block
    assert "TimeCreated,RecordId,ProviderName,Id,LevelDisplayName" in block
    assert "$event.ToXml()" not in block
    assert "Properties=(@($event.Properties)" not in block


def test_windows_service_runtime_is_vms_owned_complete_and_profile_independent():
    assert '$serviceExe = Join-Path $VenvRoot "pythonservice.exe"' in INSTALL
    assert '$servicePth = Join-Path $VenvRoot "python312._pth"' in INSTALL
    assert '"Lib\\site-packages\\win32"' in INSTALL
    assert '"Lib\\site-packages\\win32\\lib"' in INSTALL
    assert '"..\\..\\app"' in INSTALL
    assert "servicemanager*.pyd" in INSTALL
    assert "win32service*.pyd" in INSTALL
    assert "win32event*.pyd" in INSTALL
    assert "win32serviceutil.py" in INSTALL
    assert "pywin32.pth" in INSTALL
    assert "pywintypes312.dll" in INSTALL
    assert "pythoncom312.dll" in INSTALL
    assert "service-runtime-python.exe" in INSTALL
    assert "windows_service_runtime_import_ok" in INSTALL
    runtime_block = INSTALL.split("# Build a self-contained pywin32 service-host runtime", 1)[1].split("$mediaZip =", 1)[0]
    assert "$env:PATH" not in runtime_block
    assert "$env:USERPROFILE" not in runtime_block
    assert "hostedtoolcache" not in runtime_block
    assert 'Path(sys.prefix) / "pythonservice.exe"' in SERVICE_MANAGER
    assert "deploy.windows.service_host.ControlService" in SERVICE_MANAGER
    assert "deploy.windows.service_host.MediaService" in SERVICE_MANAGER


def test_windows_service_runtime_uses_sysconfig_purelib_not_site_prefix_order():
    assert 'sysconfig.get_paths()[\'purelib\']' in INSTALL
    assert "site.getsitepackages()[0]" not in INSTALL
    assert '$win32Root = Join-Path $sitePackages "win32"' in INSTALL


def test_windows_service_event_log_failure_cannot_kill_service_host():
    assert "def _safe_event_log(message: str)" in SERVICE_HOST
    assert "except Exception:" in SERVICE_HOST.split("def _safe_event_log", 1)[1].split("def _root", 1)[0]
    assert SERVICE_HOST.count("_safe_event_log(") >= 5
    assert 'servicemanager.LogInfoMsg("Intelligent VMS control API starting")' not in SERVICE_HOST
    assert 'servicemanager.LogInfoMsg("Intelligent VMS MediaMTX starting")' not in SERVICE_HOST


def test_windows_mediamtx_integrity_does_not_depend_on_powershell_module_autoload():
    assert "function Get-Sha256Hex" in INSTALL
    assert "Get-FileHash -Algorithm SHA256 -LiteralPath $mediaZip" not in INSTALL
    assert "faa97974861eb75a68b5aa326c78e7e7a6f670b5ef191bace78e715130381f23" in INSTALL


def test_windows_pywin32_layout_probe_avoids_powershell5_inline_python_quoting():
    assert 'validate-pywin32-layout.py' in INSTALL
    probe_block = INSTALL.split('$layoutProbe = Join-Path $VenvRoot "validate-pywin32-layout.py"', 1)[1].split('# Copy the CPython standard runtime', 1)[0]
    assert 'Invoke-Checked $venvPython @($layoutProbe)' in probe_block
    assert 'Invoke-Checked $venvPython @("-c",@\'' not in INSTALL
    assert 'Remove-Item -LiteralPath $layoutProbe' in probe_block


def test_windows_control_service_uses_thread_safe_selector_event_loop():
    assert "loop_factory=asyncio.SelectorEventLoop" in SERVICE_HOST
    assert "WindowsSelectorEventLoopPolicy" not in SERVICE_HOST
    assert "ProactorEventLoop" in SERVICE_HOST


def test_windows_control_service_delegates_shutdown_to_scm_not_process_signals():
    assert "class ScmUvicornServer(uvicorn.Server)" in SERVICE_HOST
    assert "def capture_signals(self)" in SERVICE_HOST
    assert "@contextlib.contextmanager" in SERVICE_HOST
    assert "yield" in SERVICE_HOST
    assert "self.server.should_exit = True" in SERVICE_HOST
    assert "self.server._serve(" not in SERVICE_HOST


def test_windows_failure_diagnostics_reuse_redacted_collector_before_event_metadata():
    failure = WORKFLOW.index("Capture Windows service failure evidence")
    cleanup = WORKFLOW.index("Cleanup ephemeral hosted-runner state")
    block = WORKFLOW[failure:cleanup]
    assert block.index("=== redacted product log tails ===") < block.index("=== recent Python Service Event 14 metadata ===")
    assert "Collect-WindowsDiagnostics.ps1" in block
    assert "failure-diag-redacted" in block
    assert "Get-Content -LiteralPath $path -Tail 100" in block
    assert '$_.ProviderName -eq "Python Service" -and $_.Id -eq 14' in block
    assert "Event14 XML" not in block
    assert "$event.ToXml()" not in block


def test_windows_recording_path_satisfies_mediamtx_filename_contract(tmp_path):
    output = tmp_path / "vms.env"
    generate(
        output,
        recording_dir=Path("D:/VMS/Recordings"),
        app_root=Path("C:/ProgramData/IntelligentVMS/app"),
        venv_python=Path("C:/ProgramData/IntelligentVMS/runtime/venv/Scripts/python.exe"),
        postgres_password="c" * 64,
        postgres_service="postgresql-x64-17",
        camera_cidrs=["127.0.0.1/32"],
    )
    body = output.read_text(encoding="utf-8")
    record_path = next(line for line in body.splitlines() if line.startswith("RECORDING_PATH_TEMPLATE="))
    assert "%path" in record_path
    assert "%s-%f" in record_path


def test_windows_programdata_execution_and_persistent_paths_are_explicitly_protected():
    for marker in (
        "Protect-Directory $Root",
        "Protect-Directory $AppRoot",
        "Protect-Directory $ConfigRoot",
        "Protect-Directory $RuntimeRoot",
        "Protect-Directory $LogsRoot",
        "Protect-Directory $BackupRoot",
        "Protect-Directory $RecordingRoot",
    ):
        assert marker in INSTALL


def test_windows_backup_hashing_does_not_depend_on_get_file_hash_autoload():
    assert "function Get-Sha256Hex" in LIFECYCLE
    assert "[Security.Cryptography.SHA256]::Create()" in LIFECYCLE
    assert "(Get-Sha256Hex $dump)" in LIFECYCLE
    assert "Get-FileHash -Algorithm SHA256 $dump" not in LIFECYCLE
