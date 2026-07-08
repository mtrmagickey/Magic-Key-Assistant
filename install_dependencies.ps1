# Magic Key Assistant - Dependency Installation Script
# This script installs system-level prerequisites for the application

Write-Host "======================================" -ForegroundColor Cyan
Write-Host "Magic Key - Dependency Installer" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan
Write-Host ""

# Check if running as Administrator
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "⚠️  This script requires Administrator privileges" -ForegroundColor Yellow
    Write-Host "Please run PowerShell as Administrator and try again" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit 1
}

# Function to check if a command exists
function Test-CommandExists {
    param($command)
    $null = Get-Command $command -ErrorAction SilentlyContinue
    return $?
}

Write-Host "Checking system dependencies..." -ForegroundColor Yellow
Write-Host ""

# 1. Check Python
Write-Host "[1/2] Python 3.12+" -ForegroundColor Cyan
if (Test-CommandExists python) {
    $pythonVersion = python --version
    Write-Host "  ✓ Already installed: $pythonVersion" -ForegroundColor Green
} else {
    Write-Host "  ⚠️  Not found - Installing Python 3.12..." -ForegroundColor Yellow
    try {
        winget install --id Python.Python.3.12 --silent --accept-source-agreements --accept-package-agreements
        Write-Host "  ✓ Python installed" -ForegroundColor Green
    } catch {
        Write-Host "  ❌ Failed to install Python" -ForegroundColor Red
        Write-Host "     Please install manually from: https://www.python.org/downloads/" -ForegroundColor Yellow
    }
}
Write-Host ""

# 2. Check Git (optional but useful)
Write-Host "[2/2] Git (optional)" -ForegroundColor Cyan
if (Test-CommandExists git) {
    $gitVersion = git --version
    Write-Host "  ✓ Already installed: $gitVersion" -ForegroundColor Green
} else {
    Write-Host "  ⚠️  Not found (optional for version control)" -ForegroundColor Yellow
    $installGit = Read-Host "  Install Git? (yes/no)"
    if ($installGit -eq "yes") {
        try {
            winget install --id Git.Git --silent --accept-source-agreements --accept-package-agreements
            Write-Host "  ✓ Git installed" -ForegroundColor Green
        } catch {
            Write-Host "  ❌ Failed to install Git" -ForegroundColor Red
        }
    }
}
Write-Host ""

Write-Host "======================================" -ForegroundColor Cyan
Write-Host "Dependency Check Complete" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next Steps:" -ForegroundColor Yellow
Write-Host "1. Run: python -m venv .venv" -ForegroundColor Gray
Write-Host "2. Run: .venv\Scripts\Activate.ps1" -ForegroundColor Gray
Write-Host "3. Run: pip install -r requirements.txt" -ForegroundColor Gray
Write-Host "4. Run: cd LeisureLLM && .\SetupDatabase_SQLite.ps1" -ForegroundColor Gray
Write-Host "5. Run: python -m admin.server" -ForegroundColor Gray
Write-Host "6. Open http://localhost:8000 to complete setup" -ForegroundColor Gray
Write-Host ""
