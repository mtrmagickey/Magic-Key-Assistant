$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$req = Join-Path $root "LeisureLLM\requirements.txt"
$wheelDir = Join-Path $root "LeisureLLM\wheels"
$python = Join-Path $root ".venv\Scripts\python.exe"
$constraints = Join-Path $root "wheelhouse-constraints.txt"

if (-not (Test-Path $req)) {
    Write-Host "requirements.txt not found at $req" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $python)) {
    Write-Host "Expected venv Python not found at $python" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $wheelDir)) {
    New-Item -ItemType Directory -Path $wheelDir | Out-Null
}

# Start from a clean wheelhouse so stale sdists do not get shipped.
Get-ChildItem -Path $wheelDir -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -eq ".whl" -or $_.Name -like "*.tar.gz" } |
    Remove-Item -Force -ErrorAction Stop

Write-Host "Building wheelhouse in $wheelDir" -ForegroundColor Cyan
Write-Host "Capturing constraints from active venv ..." -ForegroundColor Cyan

& $python -m pip freeze | Out-File -Encoding ascii $constraints
if ($LASTEXITCODE -ne 0) {
    Write-Host "Wheelhouse build failed while exporting constraints." -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "Using: $python -m pip wheel -r $req -w $wheelDir -c $constraints --prefer-binary" -ForegroundColor Cyan

& $python -m pip wheel -r $req -w $wheelDir -c $constraints --prefer-binary
if ($LASTEXITCODE -ne 0) {
    Write-Host "Wheelhouse build failed during pip wheel." -ForegroundColor Red
    exit $LASTEXITCODE
}

$sdists = Get-ChildItem -Path $wheelDir -File -Filter *.tar.gz -ErrorAction SilentlyContinue
if ($sdists) {
    Write-Host "Unexpected source archives found in wheelhouse:" -ForegroundColor Red
    $sdists | ForEach-Object { Write-Host "  $($_.Name)" -ForegroundColor Red }
    exit 1
}

Remove-Item $constraints -Force -ErrorAction SilentlyContinue

Write-Host "Wheelhouse build complete." -ForegroundColor Green
