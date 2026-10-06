[CmdletBinding()]
param(
 [string]$OutputRoot="",
 [string]$VersionManifest="",
 [string]$CommitSha=$env:GITHUB_SHA,
 [string]$Makensis="",
 [string]$MediaMTXZip=""
)
Set-StrictMode -Version Latest
$ErrorActionPreference="Stop"
$VmsRoot=(Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
if(-not $VersionManifest){$VersionManifest=Join-Path $VmsRoot "release\windows\product-version.json"}
$m=Get-Content $VersionManifest -Raw|ConvertFrom-Json
if(-not $OutputRoot){$OutputRoot=Join-Path $VmsRoot "artifacts\windows-installer"}
if(-not $CommitSha){$CommitSha="local"}
$stage=Join-Path $OutputRoot "stage"
$payload=Join-Path $stage "payload"
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $payload|Out-Null

$server=Join-Path $payload "server"
New-Item -ItemType Directory -Force -Path $server|Out-Null

# MediaMTX is a pinned installer build input. Installation itself is offline.
$mediaVersion="1.21.1-vms.1"
$mediaSha="4207f83bda020817d825fc627fedef71c60c0041838a3f3d65473e468c902046"
$mediaName="mediamtx_v$($mediaVersion)_windows_amd64.zip"
if(-not $MediaMTXZip){
  $cache=Join-Path $OutputRoot "build-inputs"
  New-Item -ItemType Directory -Force -Path $cache|Out-Null
  $MediaMTXZip=Join-Path $cache $mediaName
  if(-not (Test-Path $MediaMTXZip)){
    & python (Join-Path $VmsRoot "tools\build_mediamtx.py") --work (Join-Path $cache "source") --output $MediaMTXZip --target-os windows
    if($LASTEXITCODE -ne 0){throw "Pinned MediaMTX source build failed."}
  }
}
if(-not (Test-Path $MediaMTXZip)){throw "Pinned MediaMTX build input missing: $MediaMTXZip"}
$mediaActual=(Get-FileHash -Algorithm SHA256 -LiteralPath $MediaMTXZip).Hash.ToLowerInvariant()
if($mediaActual -ne $mediaSha){throw "Pinned MediaMTX build input checksum mismatch. expected=$mediaSha actual=$mediaActual"}
$runtimeStage=Join-Path $server "deploy\windows\runtime"
New-Item -ItemType Directory -Force -Path $runtimeStage|Out-Null
Copy-Item -LiteralPath $MediaMTXZip -Destination (Join-Path $runtimeStage $mediaName) -Force
foreach($name in @("services","deploy","migrations","web")){
  & robocopy.exe (Join-Path $VmsRoot $name) (Join-Path $server $name) /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP /XD "__pycache__" "tests"|Out-Null
  if($LASTEXITCODE -gt 7){throw "Failed to stage server asset $name"}
}
Copy-Item (Join-Path $VmsRoot "alembic.ini") $server

$clientArtifacts=Join-Path $OutputRoot "client"
& (Join-Path $VmsRoot "clients\windows\packaging\Publish-Client.ps1") -Version $m.client_package_version -OutputRoot $clientArtifacts
$clientStage=Get-ChildItem (Join-Path $clientArtifacts "stage") -Directory|Select-Object -First 1
if(-not $clientStage){throw "Client package staging output missing"}
New-Item -ItemType Directory -Force -Path (Join-Path $payload "client")|Out-Null
Copy-Item (Join-Path $clientStage.FullName "App") (Join-Path $payload "client\App") -Recurse

$effective=[ordered]@{
 product=$m.product;product_version=$m.product_version;installer_version=$m.installer_version;
 server_package_version=$m.server_package_version;client_package_version=$m.client_package_version;
 schema_baseline=$m.schema_baseline;build_commit=$CommitSha;status=$m.status
}
$effective|ConvertTo-Json|Set-Content (Join-Path $stage "product-version.json") -Encoding UTF8
Copy-Item (Join-Path $PSScriptRoot "Invoke-Setup.ps1") $stage

if(-not $Makensis){
  $pf86=[Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
  $candidates=@((Join-Path $pf86 "NSIS\makensis.exe"),(Join-Path $env:ProgramFiles "NSIS\makensis.exe"))
  $Makensis=$candidates|Where-Object{Test-Path $_}|Select-Object -First 1
}
if(-not $Makensis){throw "NSIS 3.13 compiler not found. CI installs pinned nsis 3.13.0."}
New-Item -ItemType Directory -Force -Path $OutputRoot|Out-Null
& $Makensis "/DStageRoot=$stage" "/DOutputRoot=$OutputRoot" "/DProductVersion=$($m.product_version)" "/DInstallerVersion=$($m.installer_version)" "/DBuildCommit=$CommitSha" (Join-Path $PSScriptRoot "IntelligentVMS-FieldTest.nsi")
if($LASTEXITCODE -ne 0){throw "NSIS compilation failed"}
$exe=Join-Path $OutputRoot "IntelligentVMS-FieldTest-Setup-x64-$($m.product_version).exe"
if(-not (Test-Path $exe)){throw "Installer artifact missing"}
$hash=(Get-FileHash -Algorithm SHA256 $exe).Hash.ToLowerInvariant()
"$hash  $([IO.Path]::GetFileName($exe))"|Set-Content "$exe.sha256" -Encoding ascii
Write-Host "windows_installer_build_ok artifact=$exe sha256=$hash"
