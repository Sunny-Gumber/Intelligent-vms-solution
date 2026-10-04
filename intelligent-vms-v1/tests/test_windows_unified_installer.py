from pathlib import Path
import json

ROOT=Path(__file__).parents[1]
SETUP=(ROOT/"deploy/windows/installer/Invoke-Setup.ps1").read_text(encoding="utf-8")
NSIS=(ROOT/"deploy/windows/installer/IntelligentVMS-FieldTest.nsi").read_text(encoding="utf-8")
BUILD=(ROOT/"deploy/windows/installer/Build-Installer.ps1").read_text(encoding="utf-8")
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
