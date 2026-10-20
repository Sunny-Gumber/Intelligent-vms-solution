<#
.SYNOPSIS
    Build the Intelligent VMS Windows installer (MSI).

.DESCRIPTION
    Produces a single MSI containing the control API, recording service,
    analytics worker, the WPF desktop client and the MediaMTX media gateway.

    BUILD STATUS: this script has NOT been executed. No Windows toolchain was
    available in the environment where this source was produced. Treat the
    first run as a build-bring-up exercise, not a regression.

.PREREQUISITES
    - Windows Server 2022 or Windows 11
    - .NET 8 SDK
    - Python 3.11+ (64-bit)
    - WiX Toolset v4  (dotnet tool install --global wix)
    - PowerShell 7+

.EXAMPLE
    ./build.ps1 -Version 0.1.0 -Configuration Release
#>
[CmdletBinding()]
param(
    [string]$Version = "0.1.0",
    [ValidateSet("Debug", "Release")][string]$Configuration = "Release",
    [string]$OutputDir = "$PSScriptRoot/../../release/out",
    [switch]$SkipClient
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path "$PSScriptRoot/../.."
$Staging  = Join-Path $env:TEMP "vms-stage-$Version"

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Require-Command($name, $hint) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "'$name' not found on PATH. $hint"
    }
}

Write-Step "Checking prerequisites"
Require-Command python "Install Python 3.11+ 64-bit and add it to PATH."
Require-Command wix    "Install WiX v4: dotnet tool install --global wix"
if (-not $SkipClient) {
    Require-Command dotnet "Install the .NET 8 SDK."
}

if (Test-Path $Staging) { Remove-Item $Staging -Recurse -Force }
New-Item -ItemType Directory -Path $Staging -Force | Out-Null
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

# ---------------------------------------------------------------------------
Write-Step "Staging Python services into an embedded virtual environment"
$VenvDir = Join-Path $Staging "runtime"
python -m venv $VenvDir
$VenvPython = Join-Path $VenvDir "Scripts/python.exe"

& $VenvPython -m pip install --upgrade pip wheel | Out-Null
& $VenvPython -m pip install `
    "$RepoRoot/packages/shared-contracts" `
    "$RepoRoot/services/control-api" `
    "$RepoRoot/services/recording-service" `
    "$RepoRoot/services/analytics-service"
if ($LASTEXITCODE -ne 0) { throw "dependency installation failed" }

Copy-Item "$RepoRoot/database"  (Join-Path $Staging "database")  -Recurse
Copy-Item "$RepoRoot/deploy/windows" (Join-Path $Staging "deploy") -Recurse
Copy-Item "$RepoRoot/tools"     (Join-Path $Staging "tools")     -Recurse
Copy-Item "$RepoRoot/config/vms.example.toml" (Join-Path $Staging "vms.example.toml")
Copy-Item "$RepoRoot/LICENSE","$RepoRoot/NOTICE","$RepoRoot/CHANGELOG.md" $Staging

# ---------------------------------------------------------------------------
if (-not $SkipClient) {
    Write-Step "Publishing the WPF desktop client"
    dotnet publish "$RepoRoot/apps/windows-client/IntelligentVms.Client.csproj" `
        -c $Configuration -r win-x64 --self-contained false `
        -o (Join-Path $Staging "client")
    if ($LASTEXITCODE -ne 0) { throw "client publish failed" }
}

# ---------------------------------------------------------------------------
Write-Step "Fetching third-party runtime components"
$ThirdParty = Join-Path $Staging "thirdparty"
New-Item -ItemType Directory -Path $ThirdParty -Force | Out-Null

@"
Place the following redistributables in this directory before packaging:

  ffmpeg.exe / ffprobe.exe   Confirm the LICENCE of the build you ship.
                             A GPL build imposes GPL obligations on the whole
                             distribution. See NOTICE and
                             docs/operations/third-party-licenses.md.
                             Also confirm H.264/H.265 patent-pool obligations.

  mediamtx.exe               MIT licensed.

This installer deliberately does NOT download binaries at build time, so the
contents of a release are always deliberate and auditable.
"@ | Set-Content (Join-Path $ThirdParty "README.txt")

foreach ($binary in @("ffmpeg.exe", "ffprobe.exe", "mediamtx.exe")) {
    if (-not (Test-Path (Join-Path $ThirdParty $binary))) {
        Write-Warning "$binary is missing from $ThirdParty - the MSI will be incomplete."
    }
}

# ---------------------------------------------------------------------------
Write-Step "Building the MSI with WiX"
$MsiPath = Join-Path $OutputDir "IntelligentVMS-$Version-x64.msi"

wix build `
    "$PSScriptRoot/Product.wxs" `
    -arch x64 `
    -d "Version=$Version" `
    -d "StagingDir=$Staging" `
    -o $MsiPath
if ($LASTEXITCODE -ne 0) { throw "WiX build failed" }

Write-Step "Build complete"
Write-Host "  MSI : $MsiPath"
Write-Host "  Size: $([math]::Round((Get-Item $MsiPath).Length / 1MB, 1)) MB"
Write-Host ""
Write-Host "Sign the MSI before distribution:" -ForegroundColor Yellow
Write-Host "  signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `"$MsiPath`""
