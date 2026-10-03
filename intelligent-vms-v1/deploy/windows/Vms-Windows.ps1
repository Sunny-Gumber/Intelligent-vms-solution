[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidateSet("Start","Stop","Restart","Status","Health","Backup","Restore","Diagnostics","Uninstall","PurgeDatabase")]
    [string]$Action,
    [string]$BackupFile = "",
    [string]$Confirmation = "",
    [switch]$IncludeSecrets
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Join-Path $env:ProgramData "IntelligentVMS"
$AppRoot = Join-Path $Root "app"
$ConfigRoot = Join-Path $Root "config"
$EnvFile = Join-Path $ConfigRoot "vms.env"
$VenvPython = Join-Path $Root "runtime\venv\Scripts\python.exe"
$Manager = Join-Path $AppRoot "deploy\windows\service_manager.py"
$BackupRoot = Join-Path $Root "backups"

function Read-VmsEnv {
    $values = @{}
    foreach ($raw in Get-Content -LiteralPath $EnvFile) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) { continue }
        $parts = $line.Split("=",2)
        $values[$parts[0].Trim()] = $parts[1].Trim().Trim('"')
    }
    return $values
}
function Find-PgBin([hashtable]$Values) {
    $service = $Values["WINDOWS_POSTGRES_SERVICE"]
    $version = ($service -replace "^postgresql-x64-", "")
    $candidate = Join-Path $env:ProgramFiles "PostgreSQL\$version\bin"
    if (Test-Path (Join-Path $candidate "pg_dump.exe")) { return $candidate }
    if ($env:PGBIN -and (Test-Path (Join-Path $env:PGBIN "pg_dump.exe"))) { return $env:PGBIN }
    throw "PostgreSQL client tools not found."
}
function Import-VmsEnv([hashtable]$Values) {
    foreach ($key in $Values.Keys) { [Environment]::SetEnvironmentVariable($key,$Values[$key],"Process") }
}
function Wait-Health {
    $deadline=(Get-Date).AddSeconds(60)
    do {
        Start-Sleep 2
        try {
            $health=Invoke-RestMethod "http://127.0.0.1:8000/api/v1/system/health" -TimeoutSec 5
            if($health.status -eq "ok" -and $health.media_node -eq "ok"){ return }
        } catch {}
    } while((Get-Date)-lt $deadline)
    throw "VMS health did not recover."
}

if (-not (Test-Path $EnvFile)) { throw "Protected VMS configuration is missing: $EnvFile" }
$values = Read-VmsEnv
Import-VmsEnv $values
$pgBin = Find-PgBin $values

switch ($Action) {
    "Start" {
        & $VenvPython $Manager start --postgres-service $values["WINDOWS_POSTGRES_SERVICE"]
        if($LASTEXITCODE -ne 0){throw "VMS service start failed"}
        Wait-Health
    }
    "Stop" {
        & $VenvPython $Manager stop --postgres-service $values["WINDOWS_POSTGRES_SERVICE"]
        if($LASTEXITCODE -ne 0){throw "VMS service stop failed"}
    }
    "Restart" {
        & $VenvPython $Manager restart --postgres-service $values["WINDOWS_POSTGRES_SERVICE"]
        if($LASTEXITCODE -ne 0){throw "VMS service restart failed"}
        Wait-Health
    }
    "Status" {
        & $VenvPython $Manager status --postgres-service $values["WINDOWS_POSTGRES_SERVICE"]
        Get-Service $values["WINDOWS_POSTGRES_SERVICE"],"IntelligentVMSControl","IntelligentVMSMedia" |
            Select-Object Name,Status,StartType
    }
    "Health" {
        Wait-Health
        Invoke-RestMethod "http://127.0.0.1:8000/api/v1/system/health"
        Invoke-RestMethod "http://127.0.0.1:8000/api/v1/system/healthz/ready"
    }
    "Backup" {
        $stamp=(Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
        $target=Join-Path $BackupRoot $stamp
        New-Item -ItemType Directory -Force -Path $target | Out-Null
        $previous=$env:PGPASSWORD
        try {
            $env:PGPASSWORD=$values["WINDOWS_POSTGRES_PASSWORD"]
            $dump=Join-Path $target "database.dump"
            & (Join-Path $pgBin "pg_dump.exe") -h 127.0.0.1 -U vms -d vms -Fc -f $dump
            if($LASTEXITCODE -ne 0){throw "PostgreSQL backup failed"}
            (Get-FileHash -Algorithm SHA256 $dump).Hash | Set-Content (Join-Path $target "database.dump.sha256")
        } finally { $env:PGPASSWORD=$previous }
        Copy-Item (Join-Path $ConfigRoot "mediamtx.yml") $target
        "Recording media is NOT included. Path: $($values['WINDOWS_RECORDING_ROOT'])" | Set-Content (Join-Path $target "RECORDING_MEDIA_NOT_BACKED_UP.txt")
        "ClickHouse/event history is not part of the Windows small-site profile." | Set-Content (Join-Path $target "EVENT_HISTORY_NOT_BACKED_UP.txt")
        if($IncludeSecrets){ Copy-Item $EnvFile (Join-Path $target "vms.env") }
        else { "Protected vms.env/secrets are excluded. Preserve VMS_SECRET_KEY separately." | Set-Content (Join-Path $target "SECRETS_NOT_BACKED_UP.txt") }
        Write-Host "windows_field_test_backup_ok path=$target"
    }
    "Restore" {
        if($Confirmation -ne "RESTORE_WINDOWS_FIELD_TEST_DATABASE"){throw "Restore requires -Confirmation RESTORE_WINDOWS_FIELD_TEST_DATABASE"}
        if(-not (Test-Path $BackupFile)){throw "Backup file not found"}
        & $VenvPython $Manager stop --postgres-service $values["WINDOWS_POSTGRES_SERVICE"]
        $previous=$env:PGPASSWORD
        try {
            $env:PGPASSWORD=$values["WINDOWS_POSTGRES_PASSWORD"]
            & (Join-Path $pgBin "pg_restore.exe") -h 127.0.0.1 -U vms -d vms --clean --if-exists --no-owner --no-privileges $BackupFile
            if($LASTEXITCODE -ne 0){throw "PostgreSQL restore failed"}
        } finally { $env:PGPASSWORD=$previous }
        Push-Location $AppRoot
        try { & $VenvPython -m alembic -c alembic.ini upgrade head; if($LASTEXITCODE -ne 0){throw "Migration failed after restore"} }
        finally { Pop-Location }
        & $VenvPython $Manager start --postgres-service $values["WINDOWS_POSTGRES_SERVICE"]
        Wait-Health
    }
    "Diagnostics" {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $AppRoot "deploy\windows\Collect-WindowsDiagnostics.ps1")
        if($LASTEXITCODE -ne 0){throw "Diagnostics collection failed"}
    }
    "Uninstall" {
        & $VenvPython $Manager stop --postgres-service $values["WINDOWS_POSTGRES_SERVICE"]
        & $VenvPython $Manager remove --postgres-service $values["WINDOWS_POSTGRES_SERVICE"]
        Write-Host "windows_field_test_uninstall_ok persistent_database_recordings_config_secrets_preserved=true"
    }
    "PurgeDatabase" {
        if($Confirmation -ne "DELETE_WINDOWS_FIELD_TEST_DATABASE"){throw "Database purge requires -Confirmation DELETE_WINDOWS_FIELD_TEST_DATABASE"}
        & $VenvPython $Manager stop --postgres-service $values["WINDOWS_POSTGRES_SERVICE"]
        $previous=$env:PGPASSWORD
        try {
            $env:PGPASSWORD=$values["WINDOWS_POSTGRES_PASSWORD"]
            & (Join-Path $pgBin "dropdb.exe") -h 127.0.0.1 -U vms --if-exists vms
            if($LASTEXITCODE -ne 0){throw "Database purge failed"}
        } finally { $env:PGPASSWORD=$previous }
        Write-Host "database removed; recordings and protected configuration remain untouched"
    }
}
