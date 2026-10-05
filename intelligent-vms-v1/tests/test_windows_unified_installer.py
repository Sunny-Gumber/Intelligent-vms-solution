from pathlib import Path
import json

ROOT=Path(__file__).parents[1]
SETUP=(ROOT/"deploy/windows/installer/Invoke-Setup.ps1").read_text(encoding="utf-8")
NSIS=(ROOT/"deploy/windows/installer/IntelligentVMS-FieldTest.nsi").read_text(encoding="utf-8")
BUILD=(ROOT/"deploy/windows/installer/Build-Installer.ps1").read_text(encoding="utf-8")
SERVER_INSTALL=(ROOT/"deploy/windows/Install-WindowsFieldTest.ps1").read_text(encoding="utf-8")
ENV_GENERATOR=(ROOT/"deploy/windows/generate_windows_env.py").read_text(encoding="utf-8")
MANIFEST=json.loads((ROOT/"release/windows/product-version.json").read_text(encoding="utf-8"))
ADR=(ROOT/"docs/architecture/ADR_WINDOWS_UNIFIED_FIELD_TEST_INSTALLER.md").read_text(encoding="utf-8")
OPS=(ROOT/"docs/operations/WINDOWS_UNIFIED_FIELD_TEST_SETUP.md").read_text(encoding="utf-8")

def test_version_manifest_is_single_bounded_source():
    assert MANIFEST["installer_version"].count(".")==3
    assert MANIFEST["schema_baseline"]=="0018"
    assert MANIFEST["status"]=="Release Candidate / External Qualification Pending"
    assert "product-version.json" in BUILD
    assert "ProductVersion" in NSIS and "InstallerVersion" in NSIS

def test_setup_modes_are_explicit_and_both_orders_server_first():
    assert 'ValidateSet("Server","Client","Both")' in SETUP
    assert SETUP.index('if($Components -in @("Server","Both")){Install-Server}') < SETUP.index('if($Components -in @("Client","Both")){Install-Client}')
    for mode in ("Server","Client","Both"):
        assert mode in NSIS

def test_preflight_runs_before_program_files_or_registry_mutation():
    block=NSIS.split('Section "Install"',1)[1].split('SectionEnd',1)[0]
    preflight=block.index('-Action "Preflight"')
    assert preflight < block.index('SetOutPath "$INSTDIR\\Setup"')
    assert preflight < block.index('WriteRegStr HKLM')
    assert "Administrator rights are required" in SETUP
    assert "Windows x64 is required" in SETUP

def test_path_and_downgrade_safety_contracts():
    for marker in ("UNC/network recording storage is not supported","Recording storage cannot be a drive root","must not overlap Intelligent VMS binaries or ProgramData"):
        assert marker in SETUP
    assert "Downgrade blocked:" in SETUP
    assert "Global\\IntelligentVMSUnifiedFieldTestSetup" in SETUP

def test_server_reuses_accepted_runtime_and_postgres_is_not_owned():
    assert "Install-WindowsFieldTest.ps1" in SETUP
    assert "Vms-Windows.ps1" in SETUP
    assert 'Get-Service -Name "postgresql-x64-*"' in SETUP
    assert "Setup will not hijack or auto-remove unrelated PostgreSQL instances." in SETUP
    assert "never automatically removed" in ADR

def test_client_is_machine_binary_but_user_data_remains_separate():
    assert '[Environment]::GetEnvironmentVariable("ProgramW6432")' in SETUP
    assert '$ClientRoot=Join-Path $ProgramRoot "Client"' in SETUP
    assert "LocalAppData" in ADR
    assert "Credential Manager" in ADR
    assert "Program Files" in ADR

def test_uninstall_is_non_destructive_by_default():
    uninstall=SETUP.split("function Uninstall-Managed",1)[1].split("try{",1)[0]
    removes="\n".join(line for line in uninstall.splitlines() if "Remove-Item" in line)
    assert "config" not in removes
    assert "backups" not in removes
    assert "record" not in removes.lower()
    assert "default uninstall preserved recordings config secrets backups database and per-user client data" in SETUP

def test_component_ownership_is_merged_across_component_additions():
    assert "function Merge-ComponentOwnership" in SETUP
    assert 'if($Existing -in @("Server","Client") -and $Requested -in @("Server","Client")){return "Both"}' in SETUP
    save=SETUP.split("function Save-State",1)[1].split("function Collect-FailureDiagnostics",1)[0]
    assert "components=$owned" in save
    assert "recording_root=$recordingRootToStore" in save
    assert 'elseif($state -and ($state.PSObject.Properties.Name -contains "recording_root"))' in save


def test_failed_fresh_install_has_data_safe_rollback_contract():
    assert "$HadManagedState=Test-Path $StateFile" in SETUP
    assert 'if(-not $HadManagedState -and $Action -eq "Install")' in SETUP
    assert "Uninstall-Managed" in SETUP
    assert "persistent data preserved" in SETUP
    install=NSIS.split('Section "Install"',1)[1].split('SectionEnd',1)[0]
    assert 'IfFileExists "$PROGRAMDATA\\IntelligentVMS\\installer-state.json" managed_state_present' in install
    assert 'DeleteRegKey HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\IntelligentVMS"' in install
    assert "roll-forward recovery required if schema advanced" in SETUP


def test_secret_and_firewall_boundaries():
    assert "[REDACTED]" in SETUP
    assert "CredentialUri" not in SETUP
    assert "category={0} message={1}" in SETUP
    for forbidden in ("New-NetFirewallRule","Set-NetFirewallProfile","VMS_SECRET_KEY=","AUTH_HS256_SECRET="):
        assert forbidden not in SETUP
    assert "No broad firewall rules" in ADR

def test_webview2_and_python_are_actionable_prerequisites():
    assert "msedgewebview2.exe" in SETUP
    assert "WebView2 Evergreen Runtime is required" in SETUP
    assert "Python 3.12 x64 runtime is required" in SETUP
    assert "self-contained .NET 10" in OPS

def test_nsis_and_artifact_contract():
    assert "RequestExecutionLevel admin" in NSIS
    assert "IntelligentVMS-FieldTest-Setup-x64-" in NSIS
    assert "WriteUninstaller" in NSIS
    assert "Get-FileHash -Algorithm SHA256" in BUILD
    assert "NSIS 3.13 compiler not found" in BUILD
    assert "$env:ProgramFiles(x86)" not in BUILD

def test_claims_remain_field_test_only():
    assert "Release Candidate / External Qualification Pending" in ADR
    assert "field-test installer foundation" in OPS
    assert "Windows 10/11" in OPS
    assert "not Production Qualified" in OPS

def test_postgres_admin_password_is_never_passed_on_command_line():
    assert "VMS_POSTGRES_ADMIN_PASSWORD" in NSIS
    assert "NSD_CreatePassword" in NSIS
    assert "SetEnvironmentVariable" in NSIS
    assert "/POSTGRESPASSWORD" not in NSIS
    assert "VMS_POSTGRES_ADMIN_PASSWORD=" not in NSIS
    assert "VMS_POSTGRES_ADMIN_PASSWORD=" not in SETUP


def test_postgres_bootstrap_credential_is_cleared_after_child_use():
    clear_call = 'SetEnvironmentVariable(t "VMS_POSTGRES_ADMIN_PASSWORD", p 0)'
    assert NSIS.count(clear_call) >= 2
    setup_block = NSIS.split('Section "Install"', 1)[1].split('SectionEnd', 1)[0]
    preflight = setup_block.index('-Action "Preflight"')
    actual_setup = setup_block.index('-Action "$Action"')
    assert setup_block.index(clear_call, preflight) > preflight
    assert setup_block.index(clear_call, actual_setup) > actual_setup


def test_v021_postgres_discovery_is_systemic_and_validated():
    for marker in (
        "HKLM:\\SOFTWARE\\PostgreSQL\\Installations",
        "Win32_Service",
        'ProgramW6432',
        'PGBIN',
        'Test-X64Pe',
        'FileMajorPart',
        'PostgreSQL client tools were not found in supported x64 PostgreSQL 14+ locations',
    ):
        assert marker in SERVER_INSTALL
    assert 'Sort-Object @{Expression="Preferred";Descending=$true}' in SERVER_INSTALL
    assert '"pgAdmin"' not in SERVER_INSTALL


def test_v021_mediamtx_is_bundled_and_install_path_is_offline():
    assert "mediamtx_v1.21.1_windows_amd64.zip" in BUILD
    assert "faa97974861eb75a68b5aa326c78e7e7a6f670b5ef191bace78e715130381f23" in BUILD
    assert "build-inputs" in BUILD
    assert "deploy\\windows\\runtime" in BUILD
    assert "Bundled MediaMTX runtime is missing from the installer payload." in SERVER_INSTALL
    assert "Bundled MediaMTX checksum mismatch." in SERVER_INSTALL
    assert "Invoke-WebRequest" not in SERVER_INSTALL
    assert "github.com/bluenviron/mediamtx" not in SERVER_INSTALL


def test_v021_quiet_setup_cannot_enter_hidden_read_host():
    assert '"-NonInteractive"' in SETUP
    assert "if($NonInteractive)" in SERVER_INSTALL
    assert "hidden prompting is disabled" in SERVER_INSTALL
    assert "Read-Host" in SERVER_INSTALL  # retained only for explicit manual execution
    noninteractive_check=SERVER_INSTALL.index("if($NonInteractive -and $bootstrapNeededBeforeMutation")
    runtime_install=SERVER_INSTALL.index('windows_field_test_stage=runtime_install')
    assert noninteractive_check < runtime_install


def test_v021_postgres_credentials_stay_off_command_lines_and_logs():
    assert "--postgres-password-env" in SERVER_INSTALL
    assert "VMS_BOOTSTRAP_DB_PASSWORD" in SERVER_INSTALL
    assert "--postgres-password-env" in ENV_GENERATOR
    assert "os.environ.get" in ENV_GENERATOR
    generated_block=SERVER_INSTALL.split("$dbPassword=New-RandomHex",1)[1].split("Protect-Directory $ConfigRoot",1)[0]
    assert '"--postgres-password",$dbPassword' not in generated_block
    assert "CREATE ROLE vms LOGIN PASSWORD '$dbPassword'" in SERVER_INSTALL
    assert '-c","CREATE ROLE' not in SERVER_INSTALL
    assert "bootstrap-role.sql" in SERVER_INSTALL
    assert "PGPASSWORD" in SERVER_INSTALL
    assert "VMS_POSTGRES_ADMIN_PASSWORD=" not in SETUP


def test_v021_fresh_install_retry_uses_explicit_bootstrap_state():
    for phase in ("config_generated","role_ready","database_ready","completed"):
        assert f'phase="{phase}"' in SERVER_INSTALL
    assert "$BootstrapStateFile" in SERVER_INSTALL
    assert "$needsBootstrap" in SERVER_INSTALL
    assert "vms.env" in SERVER_INSTALL
    bootstrap=SERVER_INSTALL.split("$needsBootstrap=",1)[1].split("Import-VmsEnv $EnvFile",1)[0]
    assert "bootstrapState.phase" in bootstrap
    assert "Test-Path $EnvFile" not in bootstrap or "needsBootstrap" in SERVER_INSTALL


def test_v021_external_commands_are_bounded_and_health_is_bounded():
    assert "function Invoke-Bounded" in SERVER_INSTALL
    assert "Wait-Job -Job $job -Timeout $TimeoutSeconds" in SERVER_INSTALL
    assert "timed out after $TimeoutSeconds seconds" in SERVER_INSTALL
    for seconds in (" 120"," 180"," 300"," 600"," 900"):
        assert seconds in SERVER_INSTALL
    assert "AddSeconds(60)" in SERVER_INSTALL
    assert "-TimeoutSec 5" in SERVER_INSTALL


def test_v021_live_progress_is_streamed_and_stage_markers_are_safe():
    install_server=SETUP.split("function Install-Server",1)[1].split("function Merge-ComponentOwnership",1)[0]
    assert "| ForEach-Object" in install_server
    assert "$output=&" not in install_server
    assert "Write-SafeLog \"server\" $text" in install_server
    for stage in (
        "postgres_discovery",
        "python_environment",
        "runtime_install",
        "postgres_authentication",
        "postgres_role",
        "postgres_database",
        "alembic_migration",
        "service_install",
        "service_start",
        "health_validation",
        "completed",
    ):
        assert f"windows_field_test_stage={stage}" in SERVER_INSTALL


def test_v021_version_manifest_is_021_field_test():
    assert MANIFEST["product_version"]=="0.2.1"
    assert MANIFEST["installer_version"]=="0.2.1.0"
    assert MANIFEST["status"]=="Release Candidate / External Qualification Pending"
