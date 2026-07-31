$ErrorActionPreference = 'Stop'
$packageRoot = Split-Path -Parent $PSScriptRoot
Push-Location $packageRoot
try {
    python -m pytest
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    ruff format --check src tests scripts
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    ruff check src tests scripts
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    mypy src tests
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    python scripts/check_import_boundaries.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    python scripts/check_secrets.py --path src
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    python scripts/evaluate_synthetic.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    python -c "import yaml; yaml.safe_load(open('..\\..\\deployment\\agentctl.capabilities.yaml', encoding='utf-8'))"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    python scripts/export_openapi.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    python -c "import json; d=json.load(open('schemas/mailhub.openapi.v1.json', encoding='utf-8')); assert d['openapi']=='3.1.0'; assert d['info']['version']=='1.0.0'"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    python scripts/test_migrations.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    python scripts/apply_migrations.py --dry-run --direction upgrade
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    python scripts/check_provenance.py --ledger source-ledger.yaml
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    python scripts/check_license_gate.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    agentctl integration validate --manifest ..\\..\\deployment\\agentctl.capabilities.yaml --json
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
