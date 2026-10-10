param([switch]$Web, [switch]$Build, [string]$HubUrl = 'http://127.0.0.1:8810')
$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path $PSScriptRoot -Parent
$appRoot = Join-Path $repositoryRoot 'apps\agora'
$localBinary = Join-Path $appRoot 'bin\hoard-agora.exe'
$buildBinary = Join-Path $appRoot 'src-tauri\target\release\hoard-agora.exe'
$hubAddress = [Uri]$HubUrl
if ($hubAddress.Scheme -ne 'http' -or -not $hubAddress.IsLoopback) { throw 'Agora requires the local Hub URL.' }
if ($Build) {
    Push-Location $appRoot
    try {
        if (-not (Test-Path -LiteralPath (Join-Path $appRoot 'node_modules'))) {
            & npm.cmd ci --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) { throw 'Could not install app dependencies.' }
        }
        & npm.cmd run desktop:build
        if ($LASTEXITCODE -ne 0) { throw 'Could not build the desktop application.' }
    } finally { Pop-Location }
}
if ($Web) { Start-Process ($HubUrl.TrimEnd('/') + '/agora/'); exit }
$binaryPath = if (Test-Path -LiteralPath $localBinary) { $localBinary } else { $buildBinary }
if (-not (Test-Path -LiteralPath $binaryPath)) { throw 'Desktop build is missing. Run scripts/start-agora.ps1 -Build, or use -Web.' }
$env:AGORA_HUB_URL = $HubUrl.TrimEnd('/')
Start-Process -FilePath $binaryPath -WorkingDirectory $appRoot -WindowStyle Hidden
