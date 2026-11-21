param([switch]$Web, [switch]$Build, [string]$HubUrl = 'http://127.0.0.1:8810')
$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path $PSScriptRoot -Parent
$appRoot = Join-Path $repositoryRoot 'apps\agora'
$localBinary = Join-Path $appRoot 'bin\hoard-agora.exe'
$buildBinary = Join-Path $appRoot 'src-tauri\target\release\hoard-agora.exe'
if ($env:CARGO_TARGET_DIR) {
    $buildBinary = Join-Path $env:CARGO_TARGET_DIR 'release\hoard-agora.exe'
}
$hubAddress = [Uri]$HubUrl
if ($hubAddress.Scheme -ne 'http' -or -not $hubAddress.IsLoopback) { throw 'Agora requires the local Hub URL.' }
if ($Build) {
    Push-Location $appRoot
    try {
        if (-not (Test-Path -LiteralPath (Join-Path $appRoot 'node_modules'))) {
            & npm.cmd ci --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) { throw 'Could not install app dependencies.' }
        }
        & npm.cmd run desktop:build -- -- --locked
        if ($LASTEXITCODE -ne 0) { throw 'Could not build the desktop application.' }
    } finally { Pop-Location }
}
$env:AGORA_HUB_URL = $HubUrl.TrimEnd('/')
if (-not $env:HOARD_HUB_DATA_DIR) { $env:HOARD_HUB_DATA_DIR = Join-Path $repositoryRoot 'data' }
if ($Web) {
    $key = (Get-Content -LiteralPath (Join-Path $env:HOARD_HUB_DATA_DIR 'hub-ui-launch-token') -Raw).Trim()
    $launch = Invoke-RestMethod -Method Post -Uri ($env:AGORA_HUB_URL + '/api/ui/launch') -MaximumRedirection 0 `
        -Headers @{ Authorization = 'Bearer ' + $key } -ContentType 'application/json' -Body '{}'
    Start-Process ($env:AGORA_HUB_URL + '/agora/#hub-session=' + $launch.ticket)
    exit
}
$binaryPath = if (-not $Build -and (Test-Path -LiteralPath $localBinary)) { $localBinary } else { $buildBinary }
if (-not (Test-Path -LiteralPath $binaryPath)) { throw 'Desktop build is missing. Run scripts/start-agora.ps1 -Build, or use -Web.' }
Start-Process -FilePath $binaryPath -WorkingDirectory $appRoot -WindowStyle Hidden
