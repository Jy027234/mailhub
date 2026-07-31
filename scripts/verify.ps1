$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $repoRoot 'packages/mailhub')
try {
    & (Join-Path (Get-Location) 'scripts/verify.ps1')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Pop-Location
    Push-Location $repoRoot
    python -m pytest deployment/tests/test_mailhub_agentctl_handlers.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
