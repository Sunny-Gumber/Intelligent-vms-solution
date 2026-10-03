[CmdletBinding()]
param([string]$Output = "")
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Root=Join-Path $env:ProgramData "IntelligentVMS"
$EnvFile=Join-Path $Root "config\vms.env"
if(-not (Test-Path $EnvFile)){throw "VMS configuration missing"}
if(-not $Output){$Output=Join-Path $Root ("diagnostics-" + (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ") + ".zip")}
$values=@{}
foreach($raw in Get-Content $EnvFile){$line=$raw.Trim();if($line -and -not $line.StartsWith("#") -and $line.Contains("=")){$p=$line.Split("=",2);$values[$p[0].Trim()]=$p[1].Trim().Trim('"')}}
function Redact([string]$Text){
    $result=$Text
    foreach($key in $values.Keys){
        if($key -match "SECRET|TOKEN|PASSWORD|PRIVATE_KEY"){
            $value=$values[$key]
            if($value){$result=$result.Replace($value,"[REDACTED]")}
        }
    }
    $result=[regex]::Replace($result,'(?i)Bearer\s+[A-Za-z0-9._~+\-/]+=*','Bearer [REDACTED]')
    $result=[regex]::Replace($result,'(?i)\b((?:rtsp|rtsps|http|https)://)[^/\s:@]+:[^@\s/]+@','$1[REDACTED]@')
    $result=[regex]::Replace($result,'(?i)(token|password)=([^\s&]+)','$1=[REDACTED]')
    return $result
}
$temp=Join-Path $env:TEMP ("vms-diag-"+[guid]::NewGuid())
New-Item -ItemType Directory $temp | Out-Null
try{
    (Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,OSArchitecture | Format-List | Out-String) | Set-Content (Join-Path $temp "windows.txt")
    (Get-Service "IntelligentVMSControl","IntelligentVMSMedia",$values["WINDOWS_POSTGRES_SERVICE"] -ErrorAction SilentlyContinue | Select-Object Name,Status,StartType | Format-Table | Out-String) | Set-Content (Join-Path $temp "services.txt")
    try{(Invoke-RestMethod "http://127.0.0.1:8000/api/v1/system/health" -TimeoutSec 5 | ConvertTo-Json -Depth 5)|Set-Content (Join-Path $temp "health.json")}catch{$_.Exception.GetType().Name|Set-Content (Join-Path $temp "health-error.txt")}
    try{(Invoke-RestMethod "http://127.0.0.1:8000/api/v1/system/healthz/ready" -TimeoutSec 5 | ConvertTo-Json -Depth 5)|Set-Content (Join-Path $temp "readiness.json")}catch{$_.Exception.GetType().Name|Set-Content (Join-Path $temp "readiness-error.txt")}
    $recording=$values["WINDOWS_RECORDING_ROOT"]
    (Get-Item $recording | Select-Object FullName,Attributes | Format-List | Out-String) | Set-Content (Join-Path $temp "recording-path.txt")
    $drive=(Get-Item $recording).PSDrive.Name
    (Get-PSDrive $drive | Select-Object Name,Used,Free,Root | Format-List | Out-String) | Set-Content (Join-Path $temp "recording-space.txt")
    (Get-NetFirewallRule -DisplayName "Intelligent VMS*" -ErrorAction SilentlyContinue | Select-Object DisplayName,Enabled,Direction,Action | Format-Table | Out-String) | Set-Content (Join-Path $temp "firewall.txt")
    $presence=@{};foreach($key in @("DATABASE_URL","VMS_SECRET_KEY","AUTH_HS256_SECRET","RECORDING_HOOK_TOKEN","LIVE_VIEW_TOKEN_PRIVATE_KEY_B64","WINDOWS_RECORDING_ROOT")){$presence[$key]=[bool]$values[$key]}
    ($presence|ConvertTo-Json)|Set-Content (Join-Path $temp "config-presence.json")
    foreach($name in @("control-api.log","mediamtx.log")){
        $path=Join-Path $Root "logs\$name"
        if(Test-Path $path){Redact ((Get-Content $path -Tail 500) -join [Environment]::NewLine)|Set-Content (Join-Path $temp $name)}
    }
    Get-ChildItem $temp -File | ForEach-Object {$content=Get-Content $_.FullName -Raw;Redact $content|Set-Content $_.FullName}
    Compress-Archive -Path (Join-Path $temp "*") -DestinationPath $Output -Force
    & icacls.exe $Output /inheritance:r /grant:r "*S-1-5-18:F" "*S-1-5-32-544:F" | Out-Null
    Write-Host "windows_field_test_diagnostics_ok file=$Output"
}finally{Remove-Item $temp -Recurse -Force -ErrorAction SilentlyContinue}
