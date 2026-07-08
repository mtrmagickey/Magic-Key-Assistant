$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $root "launcher.py"
$icon = Join-Path $root "MTRMK-Assistant-Icon.ico"
$versionInfo = Join-Path $root "version_info.py"
$distExe = Join-Path $root "dist\MagicKeyAssistant-Portable.exe"

if (-not (Test-Path $launcher)) {
    Write-Host "launcher.py not found at $launcher" -ForegroundColor Red
    exit 1
}

Write-Host "Building portable release exe..." -ForegroundColor Cyan

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name MagicKeyAssistant-Portable `
    --icon $icon `
    --version-file $versionInfo `
    --add-data "LeisureLLM;LeisureLLM" `
    $launcher

if ($LASTEXITCODE -ne 0) {
    Write-Host "Portable release build failed." -ForegroundColor Red
    exit $LASTEXITCODE
}

if (-not (Test-Path $distExe)) {
    Write-Host "Expected output not found: $distExe" -ForegroundColor Red
    exit 1
}

Write-Host "Portable release ready: $distExe" -ForegroundColor Green
