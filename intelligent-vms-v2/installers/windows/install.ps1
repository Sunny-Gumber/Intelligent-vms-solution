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

Assert-Administrator
Write-Host "==> 1/7 Creating directories"

foreach ($dir in @($InstallPath, $DataPath,
                   "$DataPath\recordings", "$DataPath\exports",
                   "$DataPath\snapshots", "$DataPath\logs",
                   "$DataPath\spool",     "$DataPath\config",
                   "$DataPath\keys")) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

Write-Host "==> 2/7 Restricting ACLs on the key directory"
# The master key decrypts every camera password. Only SYSTEM and local
# Administrators may read it; inheritance is removed so a permissive parent
# ACL cannot widen access later.
$acl = Get-Acl "$DataPath\keys"
$acl.SetAccessRuleProtection($true, $false)
$acl.Access | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
foreach ($who in @("NT AUTHORITY\SYSTEM", "BUILTIN\Administrators")) {
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
        $who, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")))
}
Set-Acl "$DataPath\keys" $acl

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
@"
[vms]
environment = "production"
secret_key = "$(New-SecretKey)"
database_url = "$DatabaseUrl"
data_root = "$($DataPath -replace '\\','\\')"
api_host = "127.0.0.1"
api_port = 8080
trusted_hosts = ["localhost"]      # CHANGE to the real hostname

[media]
gateway_url = "http://127.0.0.1:9997"

[recording]
segment_seconds = 300
retention_default_days = 30
"@ | Set-Content -Path $configFile -Encoding UTF8
    Write-Host "    Wrote $configFile (review trusted_hosts before going live)"
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
