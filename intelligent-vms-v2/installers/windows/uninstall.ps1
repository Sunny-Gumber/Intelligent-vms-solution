<#
.SYNOPSIS
    Removes Intelligent VMS services and program files.

.DESCRIPTION
    By default this PRESERVES ProgramData: recordings, configuration and the
    master key are left in place. Removing a VMS must never be the reason a
    customer loses their evidence archive.

    Pass -RemoveData to delete everything. You will be asked to confirm.
#>
[CmdletBinding()]
param(
    [string]$InstallPath = "C:\Program Files\Intelligent VMS",
    [string]$DataPath    = "C:\ProgramData\IntelligentVMS",
    [switch]$RemoveData
)

$ErrorActionPreference = "Stop"
$services = @("IntelligentVMS-Recorder", "IntelligentVMS-API", "IntelligentVMS-MediaMTX")

Write-Host "==> Stopping services (recorder first, to finalize open segments)"
foreach ($service in $services) {
    if (Get-Service $service -ErrorAction SilentlyContinue) {
        Stop-Service $service -Force -ErrorAction SilentlyContinue
        # Give the recorder time to close its current segments cleanly.
        if ($service -eq "IntelligentVMS-Recorder") { Start-Sleep -Seconds 10 }
        sc.exe delete $service | Out-Null
        Write-Host "    Removed $service"
    }
}

Write-Host "==> Removing program files"
if (Test-Path $InstallPath) { Remove-Item $InstallPath -Recurse -Force }

if ($RemoveData) {
    Write-Warning "This will permanently delete ALL recordings, configuration"
    Write-Warning "and the master key at: $DataPath"
    $confirm = Read-Host "Type DELETE to confirm"
    if ($confirm -ceq "DELETE") {
        Remove-Item $DataPath -Recurse -Force
        Write-Host "Data removed."
    } else {
        Write-Host "Data preserved (confirmation not given)."
    }
} else {
    Write-Host ""
    Write-Host "Data preserved at: $DataPath" -ForegroundColor Yellow
    Write-Host "Recordings, configuration and the master key are intact."
    Write-Host "Re-run with -RemoveData to delete them."
}
