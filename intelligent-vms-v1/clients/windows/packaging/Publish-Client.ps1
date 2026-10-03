[CmdletBinding()]
param([string]$Version="0.1.0",[string]$OutputRoot="")
Set-StrictMode -Version Latest
$ErrorActionPreference="Stop"
$ClientRoot=(Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Project=Join-Path $ClientRoot "src\IntelligentVMS.Desktop\IntelligentVMS.Desktop.csproj"
if(-not $OutputRoot){$OutputRoot=Join-Path $ClientRoot "artifacts"}
$Stage=Join-Path $OutputRoot "stage"
$Package=Join-Path $Stage "IntelligentVMS-DesktopClient-$Version-win-x64"
$App=Join-Path $Package "App"
Remove-Item $Stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $App|Out-Null
dotnet publish $Project -c Release -r win-x64 --self-contained true -p:Platform=x64 -p:Version=$Version -p:PublishReadyToRun=false -o $App
if($LASTEXITCODE -ne 0){throw "Desktop publish failed"}
Copy-Item (Join-Path $PSScriptRoot "Install-Client.ps1") $Package
Copy-Item (Join-Path $PSScriptRoot "Uninstall-Client.ps1") $Package
$packageInfo=@(
 "Intelligent VMS Desktop Client foundation",
 "Version: $Version",
 "Architecture: win-x64",
 "Package type: field-test ZIP; not final commercial installer"
)
$packageInfo|Set-Content (Join-Path $Package "PACKAGE.txt")
$Zip=Join-Path $OutputRoot "IntelligentVMS-DesktopClient-$Version-win-x64.zip"
Remove-Item $Zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $Package "*") -DestinationPath $Zip -CompressionLevel Optimal
(Get-FileHash -Algorithm SHA256 $Zip).Hash|Set-Content "$Zip.sha256"
Write-Host "windows_client_package_ok path=$Zip"
