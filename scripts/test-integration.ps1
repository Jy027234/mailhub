$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $repoRoot 'packages/mailhub')
try {
    python -m pytest
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    python scripts/test_migrations.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if (-not $env:MAILHUB_TEST_DATABASE_URL) {
        Write-Error 'integration_blocked: set MAILHUB_TEST_DATABASE_URL for PostgreSQL/object-store integration'
    }
    python scripts/test_migrations.py --live
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
