[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$OutputRoot,
  [string]$RunId="",
  [int]$Samples=6,
  [int]$IntervalSeconds=10
)
Set-StrictMode -Version Latest
$ErrorActionPreference="Stop"
if($Samples -lt 1 -or $Samples -gt 360){throw "Samples must be 1..360"}
if($IntervalSeconds -lt 1 -or $IntervalSeconds -gt 300){throw "IntervalSeconds must be 1..300"}
if(-not $RunId){$RunId="VMS-WIN-"+(Get-Date).ToUniversalTime().ToString("yyyyMMdd")+"-000"}
$root=[IO.Path]::GetFullPath($OutputRoot)
New-Item -ItemType Directory -Force -Path $root|Out-Null

$os=Get-CimInstance Win32_OperatingSystem
$cs=Get-CimInstance Win32_ComputerSystem
$cpu=Get-CimInstance Win32_Processor|Select-Object -First 1
$gpus=@(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue|ForEach-Object{"$($_.Name) [$($_.DriverVersion)]"})
$disks=@(Get-Volume -ErrorAction SilentlyContinue|Where-Object{$_.DriveLetter}|ForEach-Object{@{drive=[string]$_.DriveLetter;filesystem=[string]$_.FileSystem;size_bytes=[int64]$_.Size;free_bytes=[int64]$_.SizeRemaining}})
$nics=@(Get-NetAdapter -Physical -ErrorAction SilentlyContinue|ForEach-Object{"$($_.Name):$($_.InterfaceDescription):$($_.Status)"})
$security="unknown"
try{$p=Get-MpComputerStatus -ErrorAction Stop;$security="Microsoft Defender enabled=$($p.AntivirusEnabled) realtime=$($p.RealTimeProtectionEnabled)"}catch{}
$systemDrive=$env:SystemDrive.TrimEnd(":")
$systemVolume=$disks|Where-Object{$_.drive -eq $systemDrive}|Select-Object -First 1
if(-not $systemVolume){$systemVolume=@{drive=$systemDrive;filesystem="unknown";size_bytes=0;free_bytes=0}}
$manifest=[ordered]@{
  machine_id="Q-"+([guid]::NewGuid().ToString("N").Substring(0,12))
  manufacturer=[string]$cs.Manufacturer
  model=[string]$cs.Model
  cpu=[string]$cpu.Name
  cores=[int]$cpu.NumberOfCores
  threads=[int]$cpu.NumberOfLogicalProcessors
  ram_gb=[math]::Round($cs.TotalPhysicalMemory/1GB,2)
  gpu=($gpus -join "; ")
  system_disk=$systemVolume
  recording_disk=$null
  filesystem=[string]$systemVolume.filesystem
  windows=[ordered]@{edition=[string]$os.Caption;version=[string]$os.Version;os_build=[string]$os.BuildNumber;servicing_channel="RECORD_MANUALLY";lifecycle_status="VERIFY_CURRENT_MICROSOFT_LIFECYCLE"}
  security_product=$security
  domain_state=if($cs.PartOfDomain){"domain-joined"}else{"workgroup"}
  virtualization_state=[string]$cs.Model
  network_adapter=($nics -join "; ")
  display_configuration="COLLECT/VERIFY DURING EXTERNAL UI RUN"
  disks=$disks
}
$manifest|ConvertTo-Json -Depth 8|Set-Content (Join-Path $root "machine-manifest.json") -Encoding UTF8

$serviceStates=@()
foreach($name in @("IntelligentVMSControl","IntelligentVMSMedia")){
  $svc=Get-Service $name -ErrorAction SilentlyContinue
  $serviceStates+=[ordered]@{name=$name;status=if($svc){[string]$svc.Status}else{"not-installed"}}
}
$serviceStates|ConvertTo-Json|Set-Content (Join-Path $root "service-states.json") -Encoding UTF8

$perf=Join-Path $root "performance.csv"
"timestamp_utc,scenario,active_cameras,live_streams,recording_cameras,cpu_percent,ram_percent,working_set_mb,gpu_percent,network_rx_mbps,network_tx_mbps,disk_write_mbps,error_count"|Set-Content $perf
for($i=0;$i -lt $Samples;$i++){
  $cpuPct=(Get-Counter "\Processor(_Total)\% Processor Time").CounterSamples.CookedValue
  $mem=Get-CimInstance Win32_OperatingSystem
  $ramPct=100.0*(($mem.TotalVisibleMemorySize-$mem.FreePhysicalMemory)/$mem.TotalVisibleMemorySize)
  $procs=@(Get-Process -Name "pythonservice" -ErrorAction SilentlyContinue)
  $working=if($procs.Count -gt 0){[math]::Round((($procs|Measure-Object WorkingSet64 -Sum).Sum)/1MB,2)}else{0}
  $line="{0},MEASURED,0,0,0,{1:F2},{2:F2},{3},,, ,0,0" -f [DateTimeOffset]::UtcNow.ToString("O"),$cpuPct,$ramPct,$working
  Add-Content $perf $line
  if($i -lt $Samples-1){Start-Sleep -Seconds $IntervalSeconds}
}

$existing=Join-Path $env:ProgramData "IntelligentVMS\app\deploy\windows\Collect-WindowsDiagnostics.ps1"
if(Test-Path $existing){
  try{
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $existing|Out-Null
    $latest=Get-ChildItem (Join-Path $env:ProgramData "IntelligentVMS") -Filter "diagnostics-*.zip" -ErrorAction SilentlyContinue|Sort-Object LastWriteTime -Descending|Select-Object -First 1
    if($latest){Copy-Item $latest.FullName (Join-Path $root "product-diagnostics-redacted.zip")}
  }catch{"diagnostics_collection_failed=$($_.Exception.GetType().Name)"|Set-Content (Join-Path $root "diagnostics-error.txt")}
}
[ordered]@{qualification_run_id=$RunId;collected_utc=[DateTimeOffset]::UtcNow.ToString("O");source="HOSTED_CI_OR_EXTERNAL_MACHINE";note="No credentials/tokens intentionally collected"}|ConvertTo-Json|Set-Content (Join-Path $root "collection-metadata.json")
$bundle=Join-Path ([IO.Path]::GetDirectoryName($root)) ("qualification-diagnostics-"+$RunId+".zip")
if(Test-Path $bundle){Remove-Item -LiteralPath $bundle -Force}
Compress-Archive -Path (Join-Path $root "*") -DestinationPath $bundle -Force
Write-Host "qualification_collection_ok root=$root bundle=$bundle"
