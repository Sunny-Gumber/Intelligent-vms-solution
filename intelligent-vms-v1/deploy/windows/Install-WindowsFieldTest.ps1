[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RecordingRoot,
    [string]$CameraCidr = "192.168.0.0/16",
    [string]$PostgresServiceName = "",
    [switch]$Upgrade,
    [switch]$NonInteractive
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
function Test-X64Pe([string]$Path) {
    try {
        $stream=[IO.File]::OpenRead($Path)
        $reader=[IO.BinaryReader]::new($stream)
        try {
            if($reader.ReadUInt16() -ne 0x5A4D){return $false}
            $stream.Position=0x3C
            $peOffset=$reader.ReadInt32()
            $stream.Position=$peOffset
            if($reader.ReadUInt32() -ne 0x00004550){return $false}
            return ($reader.ReadUInt16() -eq 0x8664)
        } finally {$reader.Dispose();$stream.Dispose()}
    } catch {return $false}
}
function Add-PostgresCandidate([Collections.Generic.List[object]]$List,[string]$Path,[string]$Source,[int]$Priority,[int]$PreferredMajor) {
    if([string]::IsNullOrWhiteSpace($Path)){return}
    try{$full=[IO.Path]::GetFullPath($Path)}catch{return}
    $psql=Join-Path $full "psql.exe";$createdb=Join-Path $full "createdb.exe";$postgres=Join-Path $full "postgres.exe"
    if(-not (Test-Path $psql) -or -not (Test-Path $createdb) -or -not (Test-Path $postgres)){return}
    if(-not (Test-X64Pe $psql)){return}
    $info=[Diagnostics.FileVersionInfo]::GetVersionInfo($postgres)
    $major=[int]$info.FileMajorPart
    if($major -lt 14){return}
    $List.Add([pscustomobject]@{Path=$full;Source=$Source;Priority=$Priority;Major=$major;Preferred=if($major -eq $PreferredMajor){1}else{0}})
}
function Find-PostgresBin([string]$ServiceName) {
    $preferredMajor=0
    [void][int]::TryParse(($ServiceName -replace "^postgresql-x64-",""),[ref]$preferredMajor)
    $candidates=[Collections.Generic.List[object]]::new()
    $searched=[Collections.Generic.List[string]]::new()
    try {
        $escaped=$ServiceName.Replace("'","''")
        $svc=Get-CimInstance Win32_Service -Filter "Name='$escaped'" -ErrorAction Stop
        $searched.Add("service:$ServiceName")
        if($svc.PathName){
            $m=[regex]::Match([string]$svc.PathName,'^\s*"([^"]+\.exe)"|^\s*([^\s]+\.exe)')
            if($m.Success){
                $exe=if($m.Groups[1].Success){$m.Groups[1].Value}else{$m.Groups[2].Value}
                Add-PostgresCandidate $candidates (Split-Path $exe -Parent) "service" 400 $preferredMajor
            }
        }
    } catch {}
    foreach($regRoot in @("HKLM:\SOFTWARE\PostgreSQL\Installations","HKLM:\SOFTWARE\WOW6432Node\PostgreSQL\Installations")){
        $searched.Add("registry:$regRoot")
        foreach($key in Get-ChildItem $regRoot -ErrorAction SilentlyContinue){
            try {
                $p=Get-ItemProperty $key.PSPath
                $baseDir=[string]$p.'Base Directory'
                if($baseDir){Add-PostgresCandidate $candidates (Join-Path $baseDir "bin") "registry" 300 $preferredMajor}
            } catch {}
        }
    }
    $pf=[Environment]::GetEnvironmentVariable("ProgramW6432")
    if(-not $pf){$pf=$env:ProgramFiles}
    $standardRoot=Join-Path $pf "PostgreSQL"
    $searched.Add("programfiles:$standardRoot")
    foreach($dir in Get-ChildItem $standardRoot -Directory -ErrorAction SilentlyContinue){
        Add-PostgresCandidate $candidates (Join-Path $dir.FullName "bin") "programfiles" 200 $preferredMajor
    }
    if($env:PGBIN){
        $searched.Add("PGBIN:$env:PGBIN")
        Add-PostgresCandidate $candidates $env:PGBIN "PGBIN" 100 $preferredMajor
    }
    $best=$candidates|Sort-Object @{Expression="Preferred";Descending=$true},@{Expression="Major";Descending=$true},@{Expression="Priority";Descending=$true},Path|Select-Object -First 1
    if(-not $best){throw "PostgreSQL client tools were not found in supported x64 PostgreSQL 14+ locations. Searched: $($searched -join '; ')"}
    Write-Host "windows_field_test_stage=postgres_discovery source=$($best.Source) major=$($best.Major)"
    return [string]$best.Path
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
function Invoke-Bounded([string]$Exe,[string[]]$Arguments,[int]$TimeoutSeconds=900) {
    $job=Start-Job -ScriptBlock {
        param($Command,$ArgsList,$WorkingDirectory)
        Set-Location $WorkingDirectory
        $lines=@(& $Command @ArgsList 2>&1|ForEach-Object{[string]$_})
        [pscustomobject]@{ExitCode=$LASTEXITCODE;Lines=$lines}
    } -ArgumentList $Exe,$Arguments,(Get-Location).Path
    try {
        if(-not (Wait-Job -Job $job -Timeout $TimeoutSeconds)){
            Stop-Job $job -ErrorAction SilentlyContinue
            throw "$Exe timed out after $TimeoutSeconds seconds."
        }
        return (Receive-Job $job|Select-Object -Last 1)
    } finally {Remove-Job $job -Force -ErrorAction SilentlyContinue}
}
function Invoke-Checked([string]$Exe,[string[]]$Arguments,[int]$TimeoutSeconds=900) {
    $result=Invoke-Bounded $Exe $Arguments $TimeoutSeconds
    if($result.ExitCode -ne 0){throw "$Exe failed with exit code $($result.ExitCode)"}
    return @($result.Lines)
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
function Invoke-CiFailurePoint([string]$Stage) {
    if($env:GITHUB_ACTIONS -eq "true" -and $env:VMS_INSTALLER_TEST_FAIL_STAGE -eq $Stage){throw "CI injected installer failure at $Stage"}
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
$BootstrapStateFile = Join-Path $Root "bootstrap-state.json"
$bootstrapState=$null
if(Test-Path $BootstrapStateFile){try{$bootstrapState=Get-Content $BootstrapStateFile -Raw|ConvertFrom-Json}catch{$bootstrapState=$null}}
$bootstrapNeededBeforeMutation=(-not $Upgrade) -and ((-not (Test-Path $EnvFile)) -or (-not $bootstrapState) -or [string]$bootstrapState.phase -ne "completed")
if($NonInteractive -and $bootstrapNeededBeforeMutation -and -not $env:VMS_POSTGRES_ADMIN_PASSWORD){
    throw "PostgreSQL administrator credential is required for quiet first-time/retry bootstrap; hidden prompting is disabled."
}

foreach ($path in @($Root,$ConfigRoot,$RuntimeRoot,$LogsRoot,$BackupRoot,$RecordingRoot)) {
    New-Item -ItemType Directory -Force -Path $path | Out-Null
}
Protect-Directory $Root
Protect-Directory $ConfigRoot
Protect-Directory $RuntimeRoot
Protect-Directory $LogsRoot
Protect-Directory $BackupRoot
Protect-Directory $RecordingRoot

Write-Host "windows_field_test_stage=postgres_discovery"
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

Write-Host "windows_field_test_stage=runtime_install"
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

Write-Host "windows_field_test_stage=python_environment"
$python = Find-Python312
if (-not (Test-Path (Join-Path $VenvRoot "Scripts\python.exe"))) {
    & $python.Exe @($python.Args) -m venv $VenvRoot
    if ($LASTEXITCODE -ne 0) { throw "Failed to create isolated VMS Python environment." }
}
$venvPython = Join-Path $VenvRoot "Scripts\python.exe"
Write-Host "windows_field_test_stage=python_environment action=pip_upgrade"
Invoke-Checked $venvPython @("-m","pip","install","--upgrade","pip") 300
Write-Host "windows_field_test_stage=python_environment action=control_dependencies"
Invoke-Checked $venvPython @("-m","pip","install","-r",(Join-Path $AppRoot "services\control-api\requirements.txt")) 900
Write-Host "windows_field_test_stage=python_environment action=windows_dependencies"
Invoke-Checked $venvPython @("-m","pip","install","-r",(Join-Path $AppRoot "deploy\windows\requirements-windows.txt")) 600
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

Write-Host "windows_field_test_stage=runtime_install component=mediamtx"
$mediaZip = Join-Path $SourceRoot "deploy\windows\runtime\mediamtx_v1.21.1-vms.1_windows_amd64.zip"
$mediaSha = "4207f83bda020817d825fc627fedef71c60c0041838a3f3d65473e468c902046"
if(-not (Test-Path $mediaZip)){throw "Bundled MediaMTX runtime is missing from the installer payload."}
$actual = Get-Sha256Hex $mediaZip
if($actual -ne $mediaSha){throw "Bundled MediaMTX checksum mismatch."}
$mediaExe = Join-Path $MediaRoot "mediamtx.exe"
$mediaExeSha = "bde9af1cdf4d0d662dc93d881ea4e62c3e8b35c019b62bf68de5df9b442561ed"
if (-not (Test-Path $mediaExe) -or (Get-Sha256Hex $mediaExe) -ne $mediaExeSha) {
    if (Test-Path $MediaRoot) { Remove-Item $MediaRoot -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $MediaRoot | Out-Null
    Expand-Archive -LiteralPath $mediaZip -DestinationPath $MediaRoot -Force
}
if(-not (Test-Path $mediaExe) -or (Get-Sha256Hex $mediaExe) -ne $mediaExeSha){throw "Bundled MediaMTX runtime extraction verification failed."}

Copy-Item (Join-Path $AppRoot "deploy\windows\mediamtx.windows.yml") (Join-Path $ConfigRoot "mediamtx.yml") -Force

Invoke-CiFailurePoint "before_config"

$isFreshConfig=-not (Test-Path $EnvFile)
if($isFreshConfig){
    $dbPassword=New-RandomHex
    $previousBootstrapPassword=$env:VMS_BOOTSTRAP_DB_PASSWORD
    try {
        $env:VMS_BOOTSTRAP_DB_PASSWORD=$dbPassword
        Invoke-Checked $venvPython @(
            (Join-Path $AppRoot "deploy\windows\generate_windows_env.py"),
            "--output",$EnvFile,
            "--recording-dir",$RecordingRoot,
            "--app-root",$AppRoot,
            "--venv-python",$venvPython,
            "--postgres-password-env","VMS_BOOTSTRAP_DB_PASSWORD",
            "--postgres-service",$postgresService,
            "--camera-cidr",$CameraCidr
        ) 180
    } finally {$env:VMS_BOOTSTRAP_DB_PASSWORD=$previousBootstrapPassword}
    Protect-Directory $ConfigRoot
    @{phase="config_generated";updated_utc=[DateTimeOffset]::UtcNow.ToString("O")}|ConvertTo-Json|Set-Content $BootstrapStateFile -Encoding UTF8
    Invoke-CiFailurePoint "config_generated"
} else {
    Import-VmsEnv $EnvFile
    $dbPassword=$env:WINDOWS_POSTGRES_PASSWORD
    if(-not $dbPassword){throw "Protected VMS configuration is missing WINDOWS_POSTGRES_PASSWORD; cannot reconcile database bootstrap safely."}
}

$needsBootstrap=(-not $Upgrade) -and (-not $bootstrapState -or [string]$bootstrapState.phase -ne "completed")
if($needsBootstrap){
    Write-Host "windows_field_test_stage=postgres_authentication"
    $adminPassword=$env:VMS_POSTGRES_ADMIN_PASSWORD
    if(-not $adminPassword){
        if($NonInteractive){throw "PostgreSQL administrator credential is required for quiet first-time/retry bootstrap; hidden prompting is disabled."}
        $secure=Read-Host "PostgreSQL postgres administrator password" -AsSecureString
        $ptr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try{$adminPassword=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)}
        finally{[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)}
    }
    $previousPgPassword=$env:PGPASSWORD
    try {
        $env:PGPASSWORD=$adminPassword
        $roleResult=Invoke-Bounded (Join-Path $pgBin "psql.exe") @("-h","127.0.0.1","-U","postgres","-d","postgres","-tAc","SELECT 1 FROM pg_roles WHERE rolname='vms'") 120
        if($roleResult.ExitCode -ne 0){throw "Could not authenticate to PostgreSQL as postgres."}
        $roleFound=@($roleResult.Lines|ForEach-Object{$_.Trim()}) -contains "1"
        $sqlFile=Join-Path $RuntimeRoot "bootstrap-role.sql"
        try {
            if($roleFound){Write-Host "windows_field_test_stage=postgres_role action=update";"ALTER ROLE vms PASSWORD '$dbPassword';"|Set-Content $sqlFile -Encoding UTF8}
            else {Write-Host "windows_field_test_stage=postgres_role action=create";"CREATE ROLE vms LOGIN PASSWORD '$dbPassword';"|Set-Content $sqlFile -Encoding UTF8}
            Protect-Directory $RuntimeRoot
            Invoke-Checked (Join-Path $pgBin "psql.exe") @("-h","127.0.0.1","-U","postgres","-d","postgres","-v","ON_ERROR_STOP=1","-f",$sqlFile) 120
        } finally {Remove-Item $sqlFile -Force -ErrorAction SilentlyContinue}
        @{phase="role_ready";updated_utc=[DateTimeOffset]::UtcNow.ToString("O")}|ConvertTo-Json|Set-Content $BootstrapStateFile -Encoding UTF8
        Invoke-CiFailurePoint "role_ready"

        Write-Host "windows_field_test_stage=postgres_database"
        $dbResult=Invoke-Bounded (Join-Path $pgBin "psql.exe") @("-h","127.0.0.1","-U","postgres","-d","postgres","-tAc","SELECT 1 FROM pg_database WHERE datname='vms'") 120
        if($dbResult.ExitCode -ne 0){throw "Could not inspect PostgreSQL database state."}
        $databaseFound=@($dbResult.Lines|ForEach-Object{$_.Trim()}) -contains "1"
        if(-not $databaseFound){Invoke-Checked (Join-Path $pgBin "createdb.exe") @("-h","127.0.0.1","-U","postgres","-O","vms","vms") 120}
        @{phase="database_ready";updated_utc=[DateTimeOffset]::UtcNow.ToString("O")}|ConvertTo-Json|Set-Content $BootstrapStateFile -Encoding UTF8
        Invoke-CiFailurePoint "database_ready"
    } finally {$env:PGPASSWORD=$previousPgPassword;$adminPassword=$null}
}


Import-VmsEnv $EnvFile
$configuredRecording = [IO.Path]::GetFullPath($env:WINDOWS_RECORDING_ROOT).TrimEnd('\')
$requestedRecording = [IO.Path]::GetFullPath($RecordingRoot).TrimEnd('\')
if ($configuredRecording -ne $requestedRecording) {
    throw "RecordingRoot does not match the protected installation configuration. Use the original recording path or perform a documented migration."
}
Write-Host "windows_field_test_stage=alembic_migration"
Push-Location $AppRoot
try {
    Invoke-Checked $venvPython @("-m","alembic","-c","alembic.ini","upgrade","head") 300
} finally { Pop-Location }

Write-Host "windows_field_test_stage=service_install"
$serviceManager = Join-Path $AppRoot "deploy\windows\service_manager.py"
$existingControl = Get-Service -Name "IntelligentVMSControl" -ErrorAction SilentlyContinue
if (-not $existingControl) {
    Invoke-Checked $venvPython @($serviceManager,"install","--postgres-service",$postgresService)
}
& sc.exe failure IntelligentVMSControl reset= 3600 actions= restart/5000/restart/15000/none/0 | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Failed to configure IntelligentVMSControl recovery policy." }
& sc.exe failure IntelligentVMSMedia reset= 3600 actions= restart/5000/restart/15000/none/0 | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Failed to configure IntelligentVMSMedia recovery policy." }

Write-Host "windows_field_test_stage=service_start"
Invoke-Checked $venvPython @($serviceManager,"restart","--postgres-service",$postgresService) 180

Write-Host "windows_field_test_stage=health_validation"
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

@{phase="completed";updated_utc=[DateTimeOffset]::UtcNow.ToString("O")}|ConvertTo-Json|Set-Content $BootstrapStateFile -Encoding UTF8
Write-Host "windows_field_test_stage=completed"
Write-Host "windows_field_test_install_ok profile=windows-small-site"
Write-Host "Open http://127.0.0.1:8000 on this Windows host."
Write-Host "No Docker Desktop, WSL2, Redpanda or ClickHouse is required for this small-site field-test profile."
