# SQLite Database Setup Script for Magic Key Assistant

Write-Host "======================================" -ForegroundColor Cyan
Write-Host "Magic Key Assistant - SQLite Setup" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan
Write-Host ""

$dbPath = Join-Path $PSScriptRoot "assistant.db"
$migrationsDir = Join-Path $PSScriptRoot "migrations"

Write-Host "Database path: $dbPath" -ForegroundColor Cyan
Write-Host ""

# Check if database exists
if (Test-Path $dbPath) {
    Write-Host "⚠️  Database already exists" -ForegroundColor Yellow
    $confirm = Read-Host "Delete and recreate? This will DELETE ALL DATA! (yes/no)"
    
    if ($confirm -eq "yes") {
        Remove-Item $dbPath -Force
        Write-Host "✓ Old database deleted" -ForegroundColor Green
    } else {
        Write-Host "Keeping existing database. Will run migrations anyway (safe if tables exist)." -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Running SQLite migrations..." -ForegroundColor Yellow

# Find all .sqlite.sql files
$migrationFiles = Get-ChildItem $migrationsDir -Filter "*.sqlite.sql" | Sort-Object Name

if ($migrationFiles.Count -eq 0) {
    Write-Host "ERROR: No SQLite migration files found in $migrationsDir" -ForegroundColor Red
    Write-Host "Looking for: *.sqlite.sql files" -ForegroundColor Yellow
    exit 1
}

Write-Host "Found $($migrationFiles.Count) migration file(s):" -ForegroundColor Cyan
foreach ($file in $migrationFiles) {
    Write-Host "  - $($file.Name)" -ForegroundColor Gray
}
Write-Host ""

# Run each migration
foreach ($migration in $migrationFiles) {
    Write-Host "→ Running: $($migration.Name)" -ForegroundColor Cyan
    
    python run_migration_sqlite.py $dbPath $migration.FullName
    
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Migration failed: $($migration.Name)" -ForegroundColor Red
        exit 1
    }
    Write-Host ""
}

Write-Host "✓ All migrations complete!" -ForegroundColor Green
Write-Host ""

# Verify database
Write-Host "Verifying database..." -ForegroundColor Yellow
$fileSize = (Get-Item $dbPath).Length / 1KB
Write-Host "✓ Database file: $([math]::Round($fileSize, 2)) KB" -ForegroundColor Green

Write-Host ""
Write-Host "======================================" -ForegroundColor Cyan
Write-Host "Database setup complete!" -ForegroundColor Green
Write-Host "======================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "1. Configure .env file with your API keys" -ForegroundColor Gray
Write-Host "2. Run: .\StartBot.bat" -ForegroundColor Gray
Write-Host "3. Use /health command in Discord to verify database connection" -ForegroundColor Gray
Write-Host ""
Write-Host "Note: SQLite database is a single file at:" -ForegroundColor Cyan
Write-Host "  $dbPath" -ForegroundColor White
Write-Host ""
