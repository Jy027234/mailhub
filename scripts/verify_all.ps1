# MailHub archive: full local verification gate.
# Runs every package suite and static gate; exit code 0 = all green.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$results = @()

function Step($Name, [scriptblock]$Body) {
    Write-Host "== $Name" -ForegroundColor Cyan
    try {
        & $Body
        $script:results += [pscustomobject]@{ Name = $Name; Ok = $true }
        Write-Host "   PASS" -ForegroundColor Green
    } catch {
        $script:results += [pscustomobject]@{ Name = $Name; Ok = $false }
        Write-Host "   FAIL: $_" -ForegroundColor Red
    }
}

Step 'mailhub pytest' { Push-Location "$root\packages\mailhub"; try { python -m pytest -q --no-header *> $null; if ($LASTEXITCODE -ne 0) { throw "pytest exit $LASTEXITCODE" } } finally { Pop-Location } }
Step 'mailhub ruff' { Push-Location "$root\packages\mailhub"; try { ruff check src tests scripts *> $null; if ($LASTEXITCODE -ne 0) { throw "ruff exit $LASTEXITCODE" } } finally { Pop-Location } }
Step 'mailhub format' { Push-Location "$root\packages\mailhub"; try { ruff format --check src tests scripts *> $null; if ($LASTEXITCODE -ne 0) { throw "format exit $LASTEXITCODE" } } finally { Pop-Location } }
Step 'mailhub mypy' { Push-Location "$root\packages\mailhub"; try { mypy src tests *> $null; if ($LASTEXITCODE -ne 0) { throw "mypy exit $LASTEXITCODE" } } finally { Pop-Location } }
Step 'mailhub script gates' { Push-Location "$root\packages\mailhub"; try { python scripts/check_import_boundaries.py *> $null; python scripts/check_secrets.py --path src *> $null; python scripts/evaluate_synthetic.py *> $null; python scripts/test_migrations.py *> $null; python scripts/check_provenance.py --ledger source-ledger.yaml *> $null; python scripts/check_license_gate.py *> $null; if ($LASTEXITCODE -ne 0) { throw "script gate exit $LASTEXITCODE" } } finally { Pop-Location } }
Step 'deployment tests' { $env:PYTHONPATH = "$root\deployment"; Push-Location $root; try { python -m pytest -q deployment/tests --no-header *> $null; if ($LASTEXITCODE -ne 0) { throw "deployment exit $LASTEXITCODE" } } finally { Pop-Location } }
Step 'local-host pytest' { Push-Location "$root\local-host"; try { python -m pytest -q tests --no-header *> $null; if ($LASTEXITCODE -ne 0) { throw "local-host pytest exit $LASTEXITCODE" } } finally { Pop-Location } }
Step 'local-host ruff+format' { Push-Location "$root\local-host"; try { ruff check local_host tests scripts *> $null; ruff format --check local_host tests scripts *> $null; if ($LASTEXITCODE -ne 0) { throw "ruff exit $LASTEXITCODE" } } finally { Pop-Location } }
Step 'local-host mypy' { Push-Location "$root\local-host"; try { python -m mypy local_host --ignore-missing-imports *> $null; if ($LASTEXITCODE -ne 0) { throw "mypy exit $LASTEXITCODE" } } finally { Pop-Location } }

Write-Host ''
$results | Format-Table -AutoSize
$failed = @($results | Where-Object { -not $_.Ok }).Count
if ($failed -gt 0) { Write-Host "$failed step(s) FAILED" -ForegroundColor Red; exit 1 }
Write-Host 'ALL GATES GREEN' -ForegroundColor Green
exit 0
