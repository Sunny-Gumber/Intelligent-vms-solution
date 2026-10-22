<#
.SYNOPSIS
    Installs Intelligent VMS on Windows Server 2022/2025 or Windows 11.

.DESCRIPTION
    Creates directories with correct ACLs, installs the Python packages,
    writes configuration with a generated secret key, applies database
    migrations and registers three Windows services with correct dependency
    ordering.

.NOTES
    STATUS: NOT EXECUTED. No Windows host was available during development.
    Treat this as a first-install runbook to be corrected on first use.
#>
[CmdletBinding()]
param(
    [string]$InstallPath = "C:\Program Files\Intelligent VMS",
    [string]$DataPath    = "C:\ProgramData\IntelligentVMS",
    [Parameter(Mandatory = $true)][string]$DatabaseUrl,
    [string]$SourcePath  = (Resolve-Path "$PSScriptRoot\..\.."),
    [string]$PythonExe   = "python",
    [switch]$SkipServices
)

$ErrorActionPreference = "Stop"

function Assert-Administrator {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script from an elevated PowerShell session."
    }
}

function New-SecretKey {
    $bytes = New-Object byte[] 48
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return [Convert]::ToBase64String($bytes).Replace('+','-').Replace('/','_').TrimEnd('=')
}

function Set-PrivateDirectoryAcl([string]$Path) {
    $acl = Get-Acl $Path
    $acl.SetAccessRuleProtection($true, $false)
    $acl.Access | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
    foreach ($who in @("NT AUTHORITY\SYSTEM", "BUILTIN\Administrators")) {
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            $who, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")))
    }
    Set-Acl $Path $acl
}

Assert-Administrator
Write-Host "==> 1/7 Creating directories"

foreach ($dir in @($InstallPath, $DataPath,
                   "$DataPath\recordings", "$DataPath\exports",
                   "$DataPath\snapshots", "$DataPath\logs",
                   "$DataPath\spool",     "$DataPath\config",
                   "$DataPath\keys")) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

Write-Host "==> 2/7 Restricting ACLs on secret directories"
# The master key decrypts every camera password. Only SYSTEM and local
# Administrators may read it; inheritance is removed so a permissive parent
# ACL cannot widen access later.
Set-PrivateDirectoryAcl "$DataPath\keys"
Set-PrivateDirectoryAcl "$DataPath\config"

Write-Host "==> 3/7 Creating the virtual environment"
& $PythonExe -m venv "$InstallPath\venv"
$venvPython = "$InstallPath\venv\Scripts\python.exe"
& $venvPython -m pip install --upgrade pip --quiet

Write-Host "==> 4/7 Installing packages"
& $venvPython -m pip install --quiet `
    "$SourcePath\packages\shared-contracts" `
    "$SourcePath\services\control-api" `
    "$SourcePath\services\recording-service"
Copy-Item "$SourcePath\database" -Destination "$InstallPath\database" -Recurse -Force

Write-Host "==> 5/7 Writing configuration"
$configFile = "$DataPath\config\vms.toml"
if (Test-Path $configFile) {
    Write-Host "    Existing configuration kept: $configFile"
} else {
$hookSecret = New-SecretKey
$gatewayPassword = New-SecretKey
$configText = @"
[vms]
environment = "production"
secret_key = "$(New-SecretKey)"
database_url = "$DatabaseUrl"
data_root = "$($DataPath -replace '\\','\\')"
api_host = "127.0.0.1"
api_port = 8080
trusted_hosts = ["localhost", "127.0.0.1"] # Add the real hostname if needed

[media]
gateway_url = "http://127.0.0.1:9997"
gateway_user = "vms-control"
gateway_password = "$gatewayPassword"
auth_hook_user = "mediamtx"
auth_hook_secret = "$hookSecret"
auth_allowed_callers = ["127.0.0.1/32"]

[recording]
segment_seconds = 300
retention_default_days = 30
"@
    [System.IO.File]::WriteAllText(
        $configFile, $configText, [System.Text.UTF8Encoding]::new($false))
    Write-Host "    Wrote $configFile (review trusted_hosts before going live)"
}

Write-Host "    Validating production and media-hook settings"
$env:VMS_CONFIG_FILE = $configFile
$configCheck = & "$InstallPath\venv\Scripts\python.exe" -c `
    "from app.config import get_settings; s=get_settings(); p=s.media_auth_problems(); assert not p, '; '.join(p); assert '127.0.0.1' in s.trusted_hosts"
if ($LASTEXITCODE -ne 0) {
    throw "Production media-auth configuration is invalid; services were not registered."
}
$hookSecret = & "$InstallPath\venv\Scripts\python.exe" -c `
    "from app.config import get_settings; print(get_settings().media_auth_hook_secret)"
if ($LASTEXITCODE -ne 0 -or -not $hookSecret) {
    throw "Could not read the media-hook secret from $configFile."
}
$gatewayConfigPath = "$DataPath\config\mediamtx.yml"
$callbackUrl = "http://" + "mediamtx:" + $hookSecret + "@127.0.0.1:8080/internal/media/auth"
if (-not (Test-Path $gatewayConfigPath)) {
    $gatewayConfig = Get-Content "$SourcePath\deploy\docker\mediamtx.yml" -Raw
    $gatewayConfig = $gatewayConfig.Replace(
        "authHTTPAddress: http://127.0.0.1:9/media-auth-not-configured",
        "authHTTPAddress: $callbackUrl")
    $gatewayConfig = $gatewayConfig.Replace("apiAddress: :9997", "apiAddress: 127.0.0.1:9997")
    [System.IO.File]::WriteAllText(
        $gatewayConfigPath, $gatewayConfig, [System.Text.UTF8Encoding]::new($false))
}
$gatewayConfig = Get-Content $gatewayConfigPath -Raw
if (-not $gatewayConfig.Contains(
        "authHTTPAddress: $callbackUrl") -or
    -not $gatewayConfig.Contains("apiAddress: 127.0.0.1:9997")) {
    throw "MediaMTX config does not match the hook secret or loopback API; services were not registered."
}

Write-Host "==> 6/7 Applying database migrations"
Push-Location "$InstallPath\database"
$env:VMS_CONFIG_FILE = $configFile
& "$InstallPath\venv\Scripts\alembic.exe" upgrade head
Pop-Location

if ($SkipServices) {
    Write-Host "Services skipped (-SkipServices). Installation complete."
    exit 0
}

Write-Host "==> 7/7 Registering services"
# The official gateway binary is installed separately from the source package.
if (-not (Test-Path "$InstallPath\mediamtx\mediamtx.exe")) {
    throw "Install pinned MediaMTX 1.21.2 at $InstallPath\mediamtx\mediamtx.exe before registering services."
}
# Delayed auto-start lets PostgreSQL finish initialising first; the explicit
# depend= chain enforces ordering, and failure actions restart rather than
# leaving the site without recording.
sc.exe create "IntelligentVMS-MediaMTX" `
    binPath= "`"$InstallPath\mediamtx\mediamtx.exe`" `"$DataPath\config\mediamtx.yml`"" `
    start= delayed-auto DisplayName= "Intelligent VMS Media Gateway" | Out-Null

sc.exe create "IntelligentVMS-API" `
    binPath= "`"$venvPython`" -m app.main" `
    start= delayed-auto depend= "postgresql-x64-16/IntelligentVMS-MediaMTX" `
    DisplayName= "Intelligent VMS Control API" | Out-Null

sc.exe create "IntelligentVMS-Recorder" `
    binPath= "`"$venvPython`" -m recorder.worker" `
    start= delayed-auto depend= "IntelligentVMS-API" `
    DisplayName= "Intelligent VMS Recorder" | Out-Null

foreach ($service in @("IntelligentVMS-MediaMTX","IntelligentVMS-API","IntelligentVMS-Recorder")) {
    sc.exe failure $service reset= 86400 actions= restart/5000/restart/10000/restart/30000 | Out-Null
    [Environment]::SetEnvironmentVariable("VMS_CONFIG_FILE", $configFile, "Machine")
    Start-Service $service
}

Write-Host ""
Write-Host "Installation complete." -ForegroundColor Green
Write-Host "First-run credentials: $DataPath\first-run-credentials.txt"
Write-Host "Sign in, change the password, then DELETE that file."
Write-Host "API: http://localhost:8080/api/v1/docs"
