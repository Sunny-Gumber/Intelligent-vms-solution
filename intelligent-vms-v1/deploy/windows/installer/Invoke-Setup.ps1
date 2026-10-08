[CmdletBinding()]
param(
  [ValidateSet("Install","Upgrade","Repair","Uninstall","Preflight")][string]$Action="Install",
  [ValidateSet("Server","Client","Both")][string]$Components="Both",
  [string]$RecordingRoot="",
  [Parameter(Mandatory=$true)][string]$PayloadRoot,
  [Parameter(Mandatory=$true)][string]$Version,
  [string]$Commit="unknown",
  [switch]$Quiet
)
Set-StrictMode -Version Latest
$ErrorActionPreference="Stop"

$MachineProgramFiles=[Environment]::GetEnvironmentVariable("ProgramW6432")
if(-not $MachineProgramFiles){$MachineProgramFiles=$env:ProgramFiles}
$ProgramRoot=Join-Path $MachineProgramFiles "Intelligent VMS"
$ClientRoot=Join-Path $ProgramRoot "Client"
$ServerRoot=Join-Path $env:ProgramData "IntelligentVMS"
$StateFile=Join-Path $ServerRoot "installer-state.json"
$LogRoot=Join-Path $ServerRoot "installer-logs"
$mutex=$null
$HadManagedState=Test-Path $StateFile

function Write-SafeLog([string]$Stage,[string]$Message){
  New-Item -ItemType Directory -Force -Path $LogRoot|Out-Null
  $safe=$Message -replace '(?i)Bearer\s+[A-Za-z0-9._~+\\/-]+=*','Bearer [REDACTED]'
  $safe=$safe -replace '(?i)\b((?:rtsp|rtsps|http|https)://)[^/\s:@]+:[^@\s/]+@','$1[REDACTED]@'
  $safe=$safe -replace '(?i)(password|secret|token|authorization|credential|private[_ -]?key)\s*[:=]\s*\S+','$1=[REDACTED]'
  $line=("{0:o} stage={1} {2}" -f [DateTimeOffset]::UtcNow,$Stage,$safe)
  Add-Content -LiteralPath (Join-Path $LogRoot "setup.log") -Value $line -Encoding UTF8
  Write-Host $line
}
function Assert-Admin {
  $id=[Security.Principal.WindowsIdentity]::GetCurrent()
  $p=[Security.Principal.WindowsPrincipal]::new($id)
  if(-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw "Administrator rights are required for this machine-wide field-test setup."}
}
function Get-InstalledState {
  if(-not (Test-Path $StateFile)){return $null}
  try{return Get-Content -LiteralPath $StateFile -Raw|ConvertFrom-Json}catch{return [pscustomobject]@{corrupt=$true}}
}
function Compare-Version([string]$Installed,[string]$Candidate){
  try{return ([version]$Installed).CompareTo([version]$Candidate)}catch{throw "Installed version metadata is invalid; repair manually before continuing."}
}
function Get-ExistingRecordingRoot {
  $envFile=Join-Path $ServerRoot "config\vms.env"
  if(-not (Test-Path $envFile)){return ""}
  $line=Get-Content $envFile|Where-Object{$_.StartsWith("WINDOWS_RECORDING_ROOT=")}|Select-Object -First 1
  if(-not $line){return ""}
  return $line.Substring($line.IndexOf("=")+1).Trim().Trim('"')
}
function Assert-RecordingPath([string]$Path){
  if([string]::IsNullOrWhiteSpace($Path)){throw "A recording path is required for Server/Both."}
  if($Path.StartsWith("\\")){throw "UNC/network recording storage is not supported by this field-test foundation."}
  if(-not [IO.Path]::IsPathRooted($Path)){throw "Recording path must be an absolute local path."}
  $full=[IO.Path]::GetFullPath($Path)
  $root=[IO.Path]::GetPathRoot($full)
  if($full.TrimEnd('\') -eq $root.TrimEnd('\')){throw "Recording storage cannot be a drive root."}
  foreach($owned in @($ProgramRoot,$ServerRoot)){
    if($full.StartsWith($owned,[StringComparison]::OrdinalIgnoreCase)){throw "Recording storage must not overlap Intelligent VMS binaries or ProgramData."}
  }
  $drive=New-Object IO.DriveInfo ([IO.Path]::GetPathRoot($full))
  if($drive.AvailableFreeSpace -lt 2GB){Write-SafeLog "preflight" "warning=recording_drive_low_free_space"}
}
function Assert-WebView2 {
  $pf86=[Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
  $roots=@(
    (Join-Path $pf86 "Microsoft\EdgeWebView\Application"),
    (Join-Path $env:ProgramFiles "Microsoft\EdgeWebView\Application")
  )
  $found=$false
  foreach($r in $roots){if(Test-Path $r){if(Get-ChildItem $r -Recurse -Filter msedgewebview2.exe -ErrorAction SilentlyContinue|Select-Object -First 1){$found=$true;break}}}
  if(-not $found){throw "Microsoft Edge WebView2 Evergreen Runtime is required for the desktop client. Install the official Microsoft runtime and rerun Setup."}
}
function Assert-ServerPrerequisites {
  $pyOk=$false
  foreach($cmd in @("py","python")){
    try{
      $args=if($cmd -eq "py"){@("-3.12","-c","import struct,sys; print(sys.version_info[:2], struct.calcsize('P')*8)")}else{@("-c","import struct,sys; print(sys.version_info[:2], struct.calcsize('P')*8)")}
      $v=& $cmd @args 2>$null
      if($LASTEXITCODE -eq 0 -and ($v -join " ") -match "3, 12" -and ($v -join " ") -match "64"){$pyOk=$true;break}
    }catch{}
  }
  if(-not $pyOk){throw "Python 3.12 x64 runtime is required by the accepted Windows server baseline."}
  $pg=Get-Service -Name "postgresql-x64-*" -ErrorAction SilentlyContinue|Sort-Object Name -Descending|Select-Object -First 1
  if(-not $pg){throw "Supported local PostgreSQL x64 service (14+) is required. Setup will not hijack or auto-remove unrelated PostgreSQL instances."}
  $ver=0;[void][int]::TryParse(($pg.Name -replace "^postgresql-x64-",""),[ref]$ver)
  if($ver -lt 14){throw "PostgreSQL 14 or newer is required."}
  foreach($port in @(8000,8554,8888,8889,9997,9998)){
    $listener=Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue|Select-Object -First 1
    $ours=Get-Service IntelligentVMSControl,IntelligentVMSMedia -ErrorAction SilentlyContinue
    if($listener -and -not $ours){throw "Required local VMS port $port is already in use."}
  }
}
function Invoke-Preflight {
  Assert-Admin
  if(-not [Environment]::Is64BitOperatingSystem){throw "Windows x64 is required."}
  $os=Get-CimInstance Win32_OperatingSystem
  if($os.Caption -notmatch "Windows 10|Windows 11|Windows Server 2022|Windows Server 2025"){throw "Unsupported Windows family for this field-test setup: $($os.Caption)"}
  if($Components -in @("Server","Both")){
    $existing=Get-ExistingRecordingRoot
    if(-not $RecordingRoot -and $existing){$script:RecordingRoot=$existing}
    Assert-RecordingPath $RecordingRoot
    Assert-ServerPrerequisites
  }
  if($Components -in @("Client","Both")){Assert-WebView2}
  $state=Get-InstalledState
  if($state -and ($state.PSObject.Properties.Name -contains "corrupt") -and $state.corrupt){throw "Existing Intelligent VMS installer state is corrupt; do not overwrite it blindly."}
  if($state){
    $cmp=Compare-Version ([string]$state.version) $Version
    if($cmp -gt 0){throw "Downgrade blocked: installed version $($state.version) is newer than setup $Version."}
    if($Action -eq "Install" -and $cmp -eq 0){$script:Action="Repair"}
    elseif($Action -eq "Install" -and $cmp -lt 0){$script:Action="Upgrade"}
  } elseif($Action -in @("Upgrade","Repair")) {throw "$Action requested but no managed Intelligent VMS installation was detected."}
}
function Install-Client {
  $source=Join-Path $PayloadRoot "client\App"
  if(-not (Test-Path (Join-Path $source "IntelligentVMS.Desktop.exe"))){throw "Client payload is missing."}
  Get-Process IntelligentVMS.Desktop -ErrorAction SilentlyContinue|ForEach-Object{try{$_.CloseMainWindow()|Out-Null;$_.WaitForExit(5000)}catch{};if(-not $_.HasExited){Stop-Process -Id $_.Id -Force}}
  New-Item -ItemType Directory -Force -Path $ClientRoot|Out-Null
  & robocopy.exe $source $ClientRoot /MIR /R:2 /W:1 /NFL /NDL /NJH /NJS /NP|Out-Null
  if($LASTEXITCODE -gt 7){throw "Client file deployment failed."}
  $menu=Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Intelligent VMS"
  New-Item -ItemType Directory -Force -Path $menu|Out-Null
  $shell=New-Object -ComObject WScript.Shell
  $shortcut=$shell.CreateShortcut((Join-Path $menu "Intelligent VMS Desktop.lnk"))
  $shortcut.TargetPath=Join-Path $ClientRoot "IntelligentVMS.Desktop.exe";$shortcut.WorkingDirectory=$ClientRoot;$shortcut.Save()
}
function Install-Server {
  $script=Join-Path $PayloadRoot "server\deploy\windows\Install-WindowsFieldTest.ps1"
  if(-not (Test-Path $script)){throw "Server payload is missing."}
  $args=@("-NoProfile","-ExecutionPolicy","Bypass","-File",$script,"-RecordingRoot",$RecordingRoot,"-NonInteractive")
  if($Action -in @("Upgrade","Repair")){$args+="-Upgrade"}
  Write-SafeLog "server" "stage=server_child_start"
  $previousPreference=$ErrorActionPreference
  try {
    $ErrorActionPreference="Continue"
    & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" @args 2>&1 | ForEach-Object {
      $text=[string]$_
      if($text){Write-SafeLog "server" $text}
    }
    $code=$LASTEXITCODE
  } finally {
    $ErrorActionPreference=$previousPreference
  }
  if($code -ne 0){throw "Accepted Windows server installer failed with exit $code."}
  Write-SafeLog "server" "stage=server_child_complete"
  & (Join-Path $PayloadRoot "server\deploy\windows\Vms-Windows.ps1") -Action Health
}
function Merge-ComponentOwnership([string]$Existing,[string]$Requested){
  if([string]::IsNullOrWhiteSpace($Existing)){return $Requested}
  if($Existing -eq $Requested){return $Requested}
  if($Existing -eq "Both" -or $Requested -eq "Both"){return "Both"}
  if($Existing -in @("Server","Client") -and $Requested -in @("Server","Client")){return "Both"}
  throw "Existing installer component ownership is invalid; repair manually before continuing."
}
function Save-State {
  New-Item -ItemType Directory -Force -Path $ServerRoot|Out-Null
  $state=Get-InstalledState
  $owned=if($state){Merge-ComponentOwnership ([string]$state.components) $Components}else{$Components}
  $recordingRootToStore=""
  if($owned -in @("Server","Both")){
    if($Components -in @("Server","Both")){$recordingRootToStore=$RecordingRoot}
    elseif($state -and ($state.PSObject.Properties.Name -contains "recording_root")){$recordingRootToStore=[string]$state.recording_root}
  }
  $obj=[ordered]@{version=$Version;commit=$Commit;components=$owned;recording_root=$recordingRootToStore;updated_utc=[DateTimeOffset]::UtcNow.ToString("O")}
  $obj|ConvertTo-Json|Set-Content -LiteralPath $StateFile -Encoding UTF8
}
function Collect-FailureDiagnostics([string]$Stage){
  try{
    Write-SafeLog "failure" "stage=$Stage action=$Action components=$Components"
    $diag=Join-Path $PayloadRoot "server\deploy\windows\Collect-WindowsDiagnostics.ps1"
    if((Test-Path $diag) -and (Test-Path (Join-Path $ServerRoot "config\vms.env"))){& $diag|Out-Null}
  }catch{}
}
function Uninstall-Managed {
  $state=Get-InstalledState
  $owned=if($state){[string]$state.components}else{$Components}
  if($owned -in @("Server","Both")){
    $ctl=Join-Path $ServerRoot "app\deploy\windows\Vms-Windows.ps1"
    if(Test-Path $ctl){
      & $ctl -Action Uninstall
      if($LASTEXITCODE -ne 0){throw "VMS service stop/remove failed; uninstall aborted before binary removal"}
    }
    foreach($p in @((Join-Path $ServerRoot "app"),(Join-Path $ServerRoot "runtime"))){Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue}
  }
  if($owned -in @("Client","Both")){
    Get-Process IntelligentVMS.Desktop -ErrorAction SilentlyContinue|Stop-Process -Force -ErrorAction SilentlyContinue
    Remove-Item $ClientRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Intelligent VMS\Intelligent VMS Desktop.lnk") -Force -ErrorAction SilentlyContinue
  }
  Remove-Item $StateFile -Force -ErrorAction SilentlyContinue
  Write-SafeLog "uninstall" "default uninstall preserved recordings config secrets backups database and per-user client data"
}

try{
  $created=$false
  $mutex=[Threading.Mutex]::new($true,"Global\IntelligentVMSUnifiedFieldTestSetup",[ref]$created)
  if(-not $created){throw "Another Intelligent VMS setup operation is already running."}
  if($Action -eq "Uninstall"){Uninstall-Managed;exit 0}
  Invoke-Preflight
  if($Action -eq "Preflight"){Write-SafeLog "preflight" "ok components=$Components version=$Version";exit 0}
  Write-SafeLog "install" "begin action=$Action components=$Components version=$Version commit=$Commit"
  if($Components -in @("Server","Both")){Install-Server}
  if($Components -in @("Client","Both")){Install-Client}
  Save-State
  Write-SafeLog "validate" "success action=$Action components=$Components"
}catch{
  Collect-FailureDiagnostics "setup"
  Write-SafeLog "failure" ("category={0} message={1}" -f $_.Exception.GetType().Name,$_.Exception.Message)
  if(-not $HadManagedState -and $Action -eq "Install"){
    try{
      Uninstall-Managed
      Write-SafeLog "rollback" "fresh-install rollback removed installer-owned services and binaries; persistent data preserved"
    }catch{
      Write-SafeLog "rollback" "fresh-install rollback incomplete; diagnostics retained for operator recovery"
    }
  } elseif($HadManagedState -and $Action -in @("Upgrade","Repair")){
    try{
      $ctl=Join-Path $ServerRoot "app\deploy\windows\Vms-Windows.ps1"
      if(Test-Path $ctl){& $ctl -Action Start|Out-Null}
      Write-SafeLog "recovery" "existing installation retained after failed upgrade/repair; roll-forward recovery required if schema advanced"
    }catch{
      Write-SafeLog "recovery" "existing installation recovery start failed; preserved data and diagnostics require operator roll-forward"
    }
  }
  throw
}finally{
  if($mutex){try{$mutex.ReleaseMutex()}catch{};$mutex.Dispose()}
}
