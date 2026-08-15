# Start MailHub in the durable production graph (B4).
# PostgreSQL must be running (docker compose up -d) and migrations applied:
#   cd packages/mailhub
#   python scripts/apply_migrations.py --direction upgrade
$ErrorActionPreference = 'Stop'
$localRoot = Split-Path -Parent $PSScriptRoot
$root = Split-Path -Parent $localRoot
. (Join-Path $PSScriptRoot 'load_env.ps1')

$env:PYTHONPATH = (Join-Path $root 'packages\mailhub\src')

Set-Location (Join-Path $root 'packages\mailhub')
python -m uvicorn mailhub.app:app --host 127.0.0.1 --port 8000
