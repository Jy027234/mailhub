# Start the local B4 outbox worker (shares the durable graph + PostgreSQL).
$ErrorActionPreference = 'Stop'
$localRoot = Split-Path -Parent $PSScriptRoot
$root = Split-Path -Parent $localRoot
. (Join-Path $PSScriptRoot 'load_env.ps1')

$env:PYTHONPATH = ($localRoot, (Join-Path $root 'packages\mailhub\src')) -join ';'

Set-Location $localRoot
python -m local_host.worker
