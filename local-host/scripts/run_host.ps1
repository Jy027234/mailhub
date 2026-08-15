# Start the local MailHub host (B4) with uvicorn.
$ErrorActionPreference = 'Stop'
$localRoot = Split-Path -Parent $PSScriptRoot
$root = Split-Path -Parent $localRoot
. (Join-Path $PSScriptRoot 'load_env.ps1')

$env:PYTHONPATH = ($localRoot, (Join-Path $root 'packages\mailhub\src'), (Join-Path $root 'apps\bff\src')) -join ';'

Set-Location $localRoot
python -m uvicorn local_host.serve:app --host 127.0.0.1 --port 8090
