[CmdletBinding()]
param([switch]$PurgeClientData,[string]$Confirmation="")
Set-StrictMode -Version Latest
$ErrorActionPreference="Stop"
$LocalRoot=[Environment]::GetFolderPath("LocalApplicationData")
$Target=Join-Path $LocalRoot "Programs\IntelligentVMS\Client"
$StartMenu=Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\Intelligent VMS"
Remove-Item (Join-Path $StartMenu "Intelligent VMS Desktop.lnk") -Force -ErrorAction SilentlyContinue
if(Test-Path $Target){Remove-Item $Target -Recurse -Force}
if($PurgeClientData){
 if($Confirmation -ne "DELETE_INTELLIGENT_VMS_CLIENT_DATA"){throw "Client data purge requires -Confirmation DELETE_INTELLIGENT_VMS_CLIENT_DATA"}
 $ClientData=Join-Path $LocalRoot "IntelligentVMS\Client"
 Remove-Item $ClientData -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host "windows_client_uninstall_ok server_state_untouched=true client_data_preserved=$(-not $PurgeClientData)"
