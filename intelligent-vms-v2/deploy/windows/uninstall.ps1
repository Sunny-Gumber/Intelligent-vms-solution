<#
.SYNOPSIS
    Removes Intelligent VMS services and program files.

.DESCRIPTION
    RECORDINGS AND CONFIGURATION ARE PRESERVED BY DEFAULT. Pass -RemoveData to
    delete them; you will be asked to confirm, because this destroys evidence.
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = "$env:ProgramFiles\IntelligentVMS",
    [string]$DataRoot    = "$env:ProgramData\IntelligentVMS",
    [switch]$RemoveData
)
$ErrorActionPreference = 'Stop'

foreach ($service in @("IntelligentVmsRecorder", "IntelligentVmsApi")) {
    if (Get-Service $service -ErrorAction SilentlyContinue) {
        Write-Host "==> Stopping $service"
        Stop-Service $service -Force -ErrorAction SilentlyContinue
        if (Get-Command nssm -ErrorAction SilentlyContinue) {
            & nssm remove $service confirm
        } else {
            sc.exe delete $service | Out-Null
        }
    }
}

if (Test-Path $InstallRoot) {
    Write-Host "==> Removing $InstallRoot"
    Remove-Item -Recurse -Force $InstallRoot
}

if ($RemoveData) {
    Write-Warning "This permanently deletes recordings, exports and the master key."
    Write-Warning "Without the master key, stored camera credentials are unrecoverable."
    $answer = Read-Host "Type DELETE to confirm"
    if ($answer -ceq "DELETE") {
        Remove-Item -Recurse -Force $DataRoot
        Write-Host "Data removed."
    } else {
        Write-Host "Data preserved at $DataRoot"
    }
} else {
    Write-Host "Data preserved at $DataRoot (pass -RemoveData to delete)."
}
