<#
.SYNOPSIS
    Installs Intelligent VMS as Windows services.

.DESCRIPTION
    Target platforms: Windows Server 2022 / 2025.

    STATUS: this script has NOT been executed on a Windows host in the build
    environment. Review it and run it on a test machine first.

    Services are registered with NSSM (or the built-in sc.exe when NSSM is
    absent), configured to start automatically, and given write access ONLY to
    the ProgramData data root.
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = "$env:ProgramFiles\IntelligentVMS",
    [string]$DataRoot    = "$env:ProgramData\IntelligentVMS",
    [string]$PythonExe   = "python",
    [switch]$SkipPrereqCheck
)

$ErrorActionPreference = 'Stop'

if (-not ([Security.Principal.WindowsPrincipal] `
          [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an elevated PowerShell session."
}

function Assert-Prereq {
    param([string]$Command, [string]$Hint)
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        throw "Required command '$Command' not found. $Hint"
    }
}

if (-not $SkipPrereqCheck) {
    Write-Host "==> Checking prerequisites"
    Assert-Prereq $PythonExe "Install Python 3.12 and add it to PATH."
    Assert-Prereq "ffmpeg"   "Install FFmpeg and add it to PATH (see docs/deployment/windows.md)."
}

Write-Host "==> Creating directories"
foreach ($dir in @($InstallRoot, $DataRoot, "$DataRoot\recordings", "$DataRoot\exports",
                   "$DataRoot\logs", "$DataRoot\keys", "$DataRoot\config")) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

Write-Host "==> Restricting ACLs on the key store"
# The key store holds the master key that protects every camera credential.
# Only SYSTEM and Administrators may read it.
$acl = Get-Acl "$DataRoot\keys"
$acl.SetAccessRuleProtection($true, $false)
foreach ($identity in @("NT AUTHORITY\SYSTEM", "BUILTIN\Administrators")) {
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
        $identity, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")))
}
Set-Acl "$DataRoot\keys" $acl

Write-Host "==> Copying application files"
$source = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
foreach ($item in @("packages", "services", "database", "docs")) {
    Copy-Item -Recurse -Force "$source\$item" $InstallRoot
}

Write-Host "==> Creating the virtual environment"
& $PythonExe -m venv "$InstallRoot\venv"
& "$InstallRoot\venv\Scripts\pip.exe" install --upgrade pip
& "$InstallRoot\venv\Scripts\pip.exe" install `
    "$InstallRoot\packages\shared-contracts" `
    "$InstallRoot\services\control-api" `
    "$InstallRoot\services\recording-service"

$configFile = "$DataRoot\config\vms.toml"
if (-not (Test-Path $configFile)) {
    Write-Host "==> Writing $configFile (edit before starting)"
    $secret = & "$InstallRoot\venv\Scripts\python.exe" -c "import secrets; print(secrets.token_urlsafe(48))"
    @"
# Intelligent VMS configuration. Section names map to VMS_<SECTION>_<KEY>.
environment = "production"
secret_key = "$secret"
data_root = "$DataRoot"

# REQUIRED: PostgreSQL. Production startup refuses SQLite.
database_url = "postgresql+psycopg://vms:CHANGEME@127.0.0.1:5432/vms"

# REQUIRED: explicit hosts. Production startup refuses "*".
trusted_hosts = ["vms.example.local"]

media_gateway_url = "http://127.0.0.1:9997"
"@ | Set-Content -Encoding UTF8 $configFile
}

Write-Host "==> Applying database migrations"
Push-Location "$InstallRoot\database"
$env:VMS_CONFIG_FILE = $configFile
& "$InstallRoot\venv\Scripts\python.exe" -m alembic -c alembic.ini upgrade head
Pop-Location

Write-Host "==> Registering services"
$nssm = Get-Command nssm -ErrorAction SilentlyContinue
if ($nssm) {
    & nssm install IntelligentVmsApi "$InstallRoot\venv\Scripts\python.exe" `
        "-m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8080 --log-config NUL"
    & nssm set IntelligentVmsApi AppEnvironmentExtra "VMS_CONFIG_FILE=$configFile"
    & nssm set IntelligentVmsApi Start SERVICE_AUTO_START
    & nssm set IntelligentVmsApi AppStdout "$DataRoot\logs\api.out.log"
    & nssm set IntelligentVmsApi AppStderr "$DataRoot\logs\api.err.log"

    & nssm install IntelligentVmsRecorder "$InstallRoot\venv\Scripts\python.exe" "-m recorder.worker"
    & nssm set IntelligentVmsRecorder AppEnvironmentExtra "VMS_CONFIG_FILE=$configFile"
    & nssm set IntelligentVmsRecorder Start SERVICE_AUTO_START
    & nssm set IntelligentVmsRecorder DependOnService IntelligentVmsApi
    # Recording must be allowed to finalize its current segment on stop.
    & nssm set IntelligentVmsRecorder AppStopMethodConsole 30000
} else {
    Write-Warning "NSSM not found. Registering with sc.exe; it has weaker restart semantics."
    sc.exe create IntelligentVmsApi binPath= `
        "`"$InstallRoot\venv\Scripts\python.exe`" -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8080" `
        start= auto DisplayName= "Intelligent VMS Control API" | Out-Null
    sc.exe failure IntelligentVmsApi reset= 86400 actions= restart/5000/restart/10000/restart/30000 | Out-Null
}

Write-Host ""
Write-Host "Installed. Before starting:"
Write-Host "  1. Edit $configFile (database URL, trusted hosts)."
Write-Host "  2. Create the PostgreSQL database and user."
Write-Host "  3. Open the firewall: see docs\deployment\firewall.md"
Write-Host "  4. Start-Service IntelligentVmsApi, IntelligentVmsRecorder"
Write-Host "  5. The first-run administrator password is in"
Write-Host "     $DataRoot\first-run-credentials.txt - sign in, change it, delete the file."
