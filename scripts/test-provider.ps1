param([Parameter(Mandatory = $true)][string]$Provider)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $repoRoot 'packages/mailhub')
try {
    if ($Provider -eq 'sandbox') {
        python -m pytest tests/test_connectors.py tests/test_service.py
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    } else {
        Write-Error "provider_blocked:$Provider requires an explicitly configured isolated mailbox and evidence variables"
    }
} finally {
    Pop-Location
}
