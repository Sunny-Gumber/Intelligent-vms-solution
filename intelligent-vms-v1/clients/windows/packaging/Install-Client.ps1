[CmdletBinding()]
param([string]$SourceRoot="")
Set-StrictMode -Version Latest
$ErrorActionPreference="Stop"
if(-not $SourceRoot){$SourceRoot=$PSScriptRoot}
$SourceApp=Join-Path $SourceRoot "App"
if(-not(Test-Path(Join-Path $SourceApp "IntelligentVMS.Desktop.exe"))){throw "Client package App payload missing"}
$LocalRoot=[Environment]::GetFolderPath("LocalApplicationData")
$Target=Join-Path $LocalRoot "Programs\IntelligentVMS\Client"
New-Item -ItemType Directory -Force -Path $Target|Out-Null
& robocopy.exe $SourceApp $Target /MIR /R:2 /W:1 /NFL /NDL /NJH /NJS /NP|Out-Null
if($LASTEXITCODE -gt 7){throw "Client install copy failed"}
Copy-Item (Join-Path $SourceRoot "Uninstall-Client.ps1") (Join-Path $Target "Uninstall-Client.ps1") -Force
$StartMenu=Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\Intelligent VMS"
New-Item -ItemType Directory -Force -Path $StartMenu|Out-Null
$Shell=New-Object -ComObject WScript.Shell
$Shortcut=$Shell.CreateShortcut((Join-Path $StartMenu "Intelligent VMS Desktop.lnk"))
$Shortcut.TargetPath=Join-Path $Target "IntelligentVMS.Desktop.exe"
$Shortcut.WorkingDirectory=$Target
$Shortcut.Save()
Write-Host "windows_client_install_ok per_user=true"
