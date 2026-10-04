[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RecordingRoot,
    [string]$CameraCidr = "192.168.0.0/16",
    [string]$PostgresServiceName = "",
    [switch]$Upgrade
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this field-test installer from an elevated PowerShell session."
    }
}
function New-RandomHex([int]$Bytes = 32) {
    $buffer = New-Object byte[] $Bytes
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return (($buffer | ForEach-Object { $_.ToString("x2") }) -join "")
}
function Find-Python312 {
    $candidates = @(
        @{Exe="py"; Args=@("-3.12")},
        @{Exe="python"; Args=@()}
    )
    foreach ($candidate in $candidates) {
        try {
            $version = & $candidate.Exe @($candidate.Args) -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $version.Trim() -eq "3.12") {
                return $candidate
            }
        } catch {}
    }
    throw "Python 3.12 x64 is required for this field-test baseline. Install the official x64 Python 3.12 runtime and rerun."
}
function Find-PostgresService([string]$Requested) {
    if ($Requested) {
        $service = Get-Service -Name $Requested -ErrorAction Stop
        return $service.Name
    }
    $services = Get-Service -Name "postgresql-x64-*" -ErrorAction SilentlyContinue | Sort-Object Name -Descending
    if (-not $services) {
        throw "A native PostgreSQL x64 service (14 or newer) is required for this field-test baseline. The future commercial installer will bundle/install it."
    }
    return $services[0].Name
}
function Find-PostgresBin([string]$ServiceName) {
    if ($env:PGBIN -and (Test-Path (Join-Path $env:PGBIN "psql.exe"))) { return $env:PGBIN }
    $version = ($ServiceName -replace "^postgresql-x64-", "")
    $candidate = Join-Path $env:ProgramFiles "PostgreSQL\$version\bin"
    if (Test-Path (Join-Path $candidate "psql.exe")) { return $candidate }
    $root = Join-Path $env:ProgramFiles "PostgreSQL"
    $found = Get-ChildItem $root -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        ForEach-Object { Join-Path $_.FullName "bin" } |
        Where-Object { Test-Path (Join-Path $_ "psql.exe") } |
        Select-Object -First 1
    if (-not $found) { throw "PostgreSQL client tools were not found." }
    return $found
}
function Protect-Directory([string]$Path) {
    & icacls.exe $Path /inheritance:r | Out-Null
    & icacls.exe $Path /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to apply protected ACL to $Path" }
}
function Import-VmsEnv([string]$Path) {
    foreach ($raw in Get-Content -LiteralPath $Path) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith("#")) { continue }
        $separator = $line.IndexOf("=")
        if ($separator -lt 1) { continue }
        $name = $line.Substring(0, $separator).Trim()
        $value = $line.Substring($separator + 1).Trim()
        if ($value.Length -ge 2 -and $value.StartsWith('"') -and $value.EndsWith('"')) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}
function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe failed with exit code $LASTEXITCODE" }
}
function Get-Sha256Hex([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha.ComputeHash($stream)
        return (($bytes | ForEach-Object { $_.ToString("x2") }) -join "")
    } finally {
        $sha.Dispose()
        $stream.Dispose()
    }
}

Assert-Administrator
if (-not [Environment]::Is64BitOperatingSystem) { throw "Windows x64 is required." }
$os = Get-CimInstance Win32_OperatingSystem
if ($os.Caption -notmatch "Windows 10|Windows 11|Windows Server 2022|Windows Server 2025") {
    throw "This Windows edition is outside the field-test target matrix: $($os.Caption)"
}
if ($RecordingRoot.StartsWith("\\")) { throw "UNC/network recording storage is not supported in this field-test baseline." }
if (-not [IO.Path]::IsPathRooted($RecordingRoot)) { throw "RecordingRoot must be an absolute local Windows path." }

$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Root = Join-Path $env:ProgramData "IntelligentVMS"
$AppRoot = Join-Path $Root "app"
$ConfigRoot = Join-Path $Root "config"
$RuntimeRoot = Join-Path $Root "runtime"
$LogsRoot = Join-Path $Root "logs"
$BackupRoot = Join-Path $Root "backups"
$VenvRoot = Join-Path $RuntimeRoot "venv"
$EnvFile = Join-Path $ConfigRoot "vms.env"
$MediaRoot = Join-Path $RuntimeRoot "mediamtx"

foreach ($path in @($Root,$ConfigRoot,$RuntimeRoot,$LogsRoot,$BackupRoot,$RecordingRoot)) {
    New-Item -ItemType Directory -Force -Path $path | Out-Null
}
Protect-Directory $Root
Protect-Directory $ConfigRoot
Protect-Directory $RuntimeRoot
Protect-Directory $LogsRoot
Protect-Directory $BackupRoot
Protect-Directory $RecordingRoot

$postgresService = Find-PostgresService $PostgresServiceName
Set-Service -Name $postgresService -StartupType Automatic
if ((Get-Service $postgresService).Status -ne "Running") {
    Start-Service $postgresService
}
$pgBin = Find-PostgresBin $postgresService

if ($Upgrade) {
    if (-not (Test-Path $EnvFile)) { throw "Upgrade requested but protected VMS configuration is missing." }
    & (Join-Path $SourceRoot "deploy\windows\Vms-Windows.ps1") -Action Backup
    & (Join-Path $SourceRoot "deploy\windows\Vms-Windows.ps1") -Action Stop
}

if (Test-Path $AppRoot) {
    Remove-Item -LiteralPath $AppRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $AppRoot | Out-Null
Protect-Directory $AppRoot
$copyArgs = @(
    $SourceRoot, $AppRoot, "/E", "/R:2", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
    "/XD", ".venv", "venv", "__pycache__", "field-test-backups",
    "/XF", ".env", "*.pyc", "field-test-diagnostics-*.tar.gz"
)
& robocopy.exe @copyArgs | Out-Null
if ($LASTEXITCODE -gt 7) { throw "Failed to copy VMS application assets (robocopy exit $LASTEXITCODE)." }

$python = Find-Python312
if (-not (Test-Path (Join-Path $VenvRoot "Scripts\python.exe"))) {
    & $python.Exe @($python.Args) -m venv $VenvRoot
    if ($LASTEXITCODE -ne 0) { throw "Failed to create isolated VMS Python environment." }
}
$venvPython = Join-Path $VenvRoot "Scripts\python.exe"
Invoke-Checked $venvPython @("-m","pip","install","--upgrade","pip")
Invoke-Checked $venvPython @("-m","pip","install","-r",(Join-Path $AppRoot "services\control-api\requirements.txt"))
Invoke-Checked $venvPython @("-m","pip","install","-r",(Join-Path $AppRoot "deploy\windows\requirements-windows.txt"))
$sitePackages = (& $venvPython -c "import sysconfig; print(sysconfig.get_paths()['purelib'])").Trim()
if (-not $sitePackages) { throw "Could not resolve Windows VMS virtualenv site-packages." }
$appPathFile = Join-Path $sitePackages "intelligent_vms_app.pth"
Set-Content -LiteralPath $appPathFile -Value $AppRoot -Encoding ascii
Invoke-Checked $venvPython @("-c","import deploy.windows.service_host; print('windows_service_host_import_ok')")

# Build a self-contained pywin32 service-host runtime under protected VMS ownership.
# pythonservice.exe is an embedding host; unlike the ordinary venv launcher it
# must have deterministic access to servicemanager, win32 extensions, stdlib,
# site-packages, and the VMS application when SCM starts it as LocalSystem.
$basePrefix = (& $venvPython -c "import sys; print(sys.base_prefix)").Trim()
if (-not $basePrefix) { throw "Could not resolve Python 3.12 base runtime." }
$baseLib = Join-Path $basePrefix "Lib"
$baseDlls = Join-Path $basePrefix "DLLs"
if (-not (Test-Path -LiteralPath $baseLib)) { throw "Python standard library missing: $baseLib" }
if (-not (Test-Path -LiteralPath $baseDlls)) { throw "Python DLL directory missing: $baseDlls" }

$win32Root = Join-Path $sitePackages "win32"
$win32Lib = Join-Path $win32Root "lib"
$serviceExeSource = Join-Path $win32Root "pythonservice.exe"
$pywin32Pth = Join-Path $sitePackages "pywin32.pth"
$serviceManagerPyd = Get-ChildItem -LiteralPath $win32Root -Filter "servicemanager*.pyd" -File | Select-Object -First 1
$win32ServicePyd = Get-ChildItem -LiteralPath $win32Root -Filter "win32service*.pyd" -File | Select-Object -First 1
$win32EventPyd = Get-ChildItem -LiteralPath $win32Root -Filter "win32event*.pyd" -File | Select-Object -First 1
$win32ServiceUtil = Join-Path $win32Lib "win32serviceutil.py"
$pywintypesDll = Get-ChildItem -LiteralPath (Join-Path $sitePackages "pywin32_system32") -Filter "pywintypes312.dll" -File | Select-Object -First 1
$pythoncomDll = Get-ChildItem -LiteralPath (Join-Path $sitePackages "pywin32_system32") -Filter "pythoncom312.dll" -File | Select-Object -First 1
foreach($artifact in @($serviceExeSource,$pywin32Pth,$win32ServiceUtil)){
    if(-not (Test-Path -LiteralPath $artifact)){ throw "Required pywin32 service artifact missing: $artifact" }
}
foreach($artifact in @($serviceManagerPyd,$win32ServicePyd,$win32EventPyd,$pywintypesDll,$pythoncomDll)){
    if($null -eq $artifact){ throw "Required pywin32 service runtime artifact missing." }
}

$layoutProbe = Join-Path $VenvRoot "validate-pywin32-layout.py"
@'
import importlib.util
from pathlib import Path
import sysconfig
for name in ("servicemanager","win32service","win32event","win32serviceutil","pywintypes","pythoncom"):
    spec = importlib.util.find_spec(name)
    if spec is None or spec.origin is None:
        raise SystemExit(f"missing pywin32 module: {name}")
    print(f"pywin32_layout {name}={Path(spec.origin)}")
sp = Path(sysconfig.get_paths()["purelib"])
print(f"pywin32_layout pywin32.pth={sp / 'pywin32.pth'}")
print(f"pywin32_layout win32={sp / 'win32'}")
print(f"pywin32_layout win32_lib={sp / 'win32' / 'lib'}")
'@ | Set-Content -LiteralPath $layoutProbe -Encoding ascii
try {
    Invoke-Checked $venvPython @($layoutProbe)
} finally {
    Remove-Item -LiteralPath $layoutProbe -Force -ErrorAction SilentlyContinue
}

# Copy the CPython standard runtime into the VMS-owned service environment.
& robocopy.exe $baseLib (Join-Path $VenvRoot "Lib") /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP /XD site-packages __pycache__ | Out-Null
if ($LASTEXITCODE -gt 7) { throw "Failed to stage Python standard library (robocopy exit $LASTEXITCODE)." }
& robocopy.exe $baseDlls (Join-Path $VenvRoot "DLLs") /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -gt 7) { throw "Failed to stage Python runtime DLLs (robocopy exit $LASTEXITCODE)." }

foreach($runtimeDll in Get-ChildItem -LiteralPath $basePrefix -Filter "python*.dll" -File){
    Copy-Item -LiteralPath $runtimeDll.FullName -Destination (Join-Path $VenvRoot $runtimeDll.Name) -Force
}
$pythonDll = Join-Path $VenvRoot "python312.dll"
if (-not (Test-Path -LiteralPath $pythonDll)) { throw "Python service runtime DLL missing: $pythonDll" }

$serviceExe = Join-Path $VenvRoot "pythonservice.exe"
Copy-Item -LiteralPath $serviceExeSource -Destination $serviceExe -Force
Copy-Item -LiteralPath $pywintypesDll.FullName -Destination (Join-Path $VenvRoot $pywintypesDll.Name) -Force
Copy-Item -LiteralPath $pythoncomDll.FullName -Destination (Join-Path $VenvRoot $pythoncomDll.Name) -Force

# python312._pth is the CPython-supported isolated embedded-runtime path
# contract. All entries are relative to the protected VMS runtime; no user
# profile, mutable PATH, current directory, or GitHub-hosted-runner path is used.
$servicePth = Join-Path $VenvRoot "python312._pth"
@(
    ".",
    "Lib",
    "DLLs",
    "Lib\site-packages",
    "Lib\site-packages\win32",
    "Lib\site-packages\win32\lib",
    "..\..\app"
) | Set-Content -LiteralPath $servicePth -Encoding ascii

# Validate the same python312.dll + python312._pth import contract used by
# pythonservice.exe before creating any SCM registration.
$probeExe = Join-Path $VenvRoot "service-runtime-python.exe"
$basePythonExe = Join-Path $basePrefix "python.exe"
if (-not (Test-Path -LiteralPath $basePythonExe)) { throw "Python runtime probe executable missing: $basePythonExe" }
Copy-Item -LiteralPath $basePythonExe -Destination $probeExe -Force
try {
    Invoke-Checked $probeExe @("-c","import servicemanager, win32service, win32event, win32serviceutil, deploy.windows.service_host; print('windows_service_runtime_import_ok')")
} finally {
    Remove-Item -LiteralPath $probeExe -Force -ErrorAction SilentlyContinue
}
foreach($artifact in @($serviceExe,$pythonDll,$servicePth,(Join-Path $VenvRoot "pywintypes312.dll"),(Join-Path $VenvRoot "pythoncom312.dll"))){
    if(-not (Test-Path -LiteralPath $artifact)){ throw "Prepared Windows service runtime artifact missing: $artifact" }
}

$mediaZip = Join-Path $env:TEMP "mediamtx_v1.21.1_windows_amd64.zip"
$mediaUrl = "https://github.com/bluenviron/mediamtx/releases/download/v1.21.1/mediamtx_v1.21.1_windows_amd64.zip"
$mediaSha = "faa97974861eb75a68b5aa326c78e7e7a6f670b5ef191bace78e715130381f23"
if (-not (Test-Path (Join-Path $MediaRoot "mediamtx.exe"))) {
    Invoke-WebRequest -Uri $mediaUrl -OutFile $mediaZip -UseBasicParsing
    $actual = Get-Sha256Hex $mediaZip
    if ($actual -ne $mediaSha) { throw "MediaMTX download checksum mismatch." }
    if (Test-Path $MediaRoot) { Remove-Item $MediaRoot -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $MediaRoot | Out-Null
    Expand-Archive -LiteralPath $mediaZip -DestinationPath $MediaRoot -Force
}

Copy-Item (Join-Path $AppRoot "deploy\windows\mediamtx.windows.yml") (Join-Path $ConfigRoot "mediamtx.yml") -Force

if (-not (Test-Path $EnvFile)) {
    $dbPassword = New-RandomHex
    Invoke-Checked $venvPython @(
        (Join-Path $AppRoot "deploy\windows\generate_windows_env.py"),
        "--output",$EnvFile,
        "--recording-dir",$RecordingRoot,
        "--app-root",$AppRoot,
        "--venv-python",$venvPython,
        "--postgres-password",$dbPassword,
        "--postgres-service",$postgresService,
        "--camera-cidr",$CameraCidr
    )
    Protect-Directory $ConfigRoot

    $adminPassword = $env:VMS_POSTGRES_ADMIN_PASSWORD
    if (-not $adminPassword) {
        $secure = Read-Host "PostgreSQL postgres administrator password" -AsSecureString
        $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $adminPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
    }
    $previousPgPassword = $env:PGPASSWORD
    try {
        $env:PGPASSWORD = $adminPassword
        Write-Host "windows_field_test_stage=postgres_role_lookup"
        $roleExists = & (Join-Path $pgBin "psql.exe") -h 127.0.0.1 -U postgres -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='vms'"
        if ($LASTEXITCODE -ne 0) { throw "Could not authenticate to PostgreSQL as postgres." }
        $roleFound = @($roleExists) -contains "1"
        if (-not $roleFound) {
            Write-Host "windows_field_test_stage=postgres_role_create"
            Invoke-Checked (Join-Path $pgBin "psql.exe") @("-h","127.0.0.1","-U","postgres","-d","postgres","-v","ON_ERROR_STOP=1","-c","CREATE ROLE vms LOGIN PASSWORD '$dbPassword'")
        } else {
            Write-Host "windows_field_test_stage=postgres_role_update"
            Invoke-Checked (Join-Path $pgBin "psql.exe") @("-h","127.0.0.1","-U","postgres","-d","postgres","-v","ON_ERROR_STOP=1","-c","ALTER ROLE vms PASSWORD '$dbPassword'")
        }
        Write-Host "windows_field_test_stage=postgres_database_lookup"
        $dbExists = & (Join-Path $pgBin "psql.exe") -h 127.0.0.1 -U postgres -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='vms'"
        $databaseFound = @($dbExists) -contains "1"
        if (-not $databaseFound) {
            Write-Host "windows_field_test_stage=postgres_database_create"
            Invoke-Checked (Join-Path $pgBin "createdb.exe") @("-h","127.0.0.1","-U","postgres","-O","vms","vms")
        }
    } finally {
        $env:PGPASSWORD = $previousPgPassword
        $adminPassword = $null
    }
}

Import-VmsEnv $EnvFile
$configuredRecording = [IO.Path]::GetFullPath($env:WINDOWS_RECORDING_ROOT).TrimEnd('\')
$requestedRecording = [IO.Path]::GetFullPath($RecordingRoot).TrimEnd('\')
if ($configuredRecording -ne $requestedRecording) {
    throw "RecordingRoot does not match the protected installation configuration. Use the original recording path or perform a documented migration."
}
Push-Location $AppRoot
try {
    Invoke-Checked $venvPython @("-m","alembic","-c","alembic.ini","upgrade","head")
} finally { Pop-Location }

$serviceManager = Join-Path $AppRoot "deploy\windows\service_manager.py"
$existingControl = Get-Service -Name "IntelligentVMSControl" -ErrorAction SilentlyContinue
if (-not $existingControl) {
    Invoke-Checked $venvPython @($serviceManager,"install","--postgres-service",$postgresService)
}
& sc.exe failure IntelligentVMSControl reset= 3600 actions= restart/5000/restart/15000/none/0 | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Failed to configure IntelligentVMSControl recovery policy." }
& sc.exe failure IntelligentVMSMedia reset= 3600 actions= restart/5000/restart/15000/none/0 | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Failed to configure IntelligentVMSMedia recovery policy." }

Invoke-Checked $venvPython @($serviceManager,"restart","--postgres-service",$postgresService)

$deadline = (Get-Date).AddSeconds(60)
$health = $null
do {
    Start-Sleep -Seconds 2
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/system/health" -TimeoutSec 5
        if ($health.status -eq "ok" -and $health.media_node -eq "ok") { break }
    } catch {}
} while ((Get-Date) -lt $deadline)
if (-not $health -or $health.status -ne "ok" -or $health.media_node -ne "ok") {
    throw "VMS services did not become healthy. Run Vms-Windows.ps1 -Action Diagnostics."
}

Write-Host "windows_field_test_install_ok profile=windows-small-site"
Write-Host "Open http://127.0.0.1:8000 on this Windows host."
Write-Host "No Docker Desktop, WSL2, Redpanda or ClickHouse is required for this small-site field-test profile."
