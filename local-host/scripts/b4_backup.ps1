# Backup the B4 deployment: PostgreSQL dump + host SQLite + reports.
$ErrorActionPreference = 'Stop'
$localRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'load_env.ps1') | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupDir = Join-Path $localRoot "backups\$stamp"
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

Write-Host "备份到 $backupDir"

$pgDump = Join-Path $backupDir 'postgres-mailhub.dump'
docker exec mailhub-b4-postgres pg_dump -U mailhub -d mailhub -Fc -f /tmp/mailhub.dump
if ($LASTEXITCODE -ne 0) { throw 'pg_dump failed' }
docker cp mailhub-b4-postgres:/tmp/mailhub.dump $pgDump
if ($LASTEXITCODE -ne 0) { throw 'docker cp failed' }

Copy-Item (Join-Path $localRoot 'data\local_host.db') (Join-Path $backupDir 'local_host.db') -ErrorAction SilentlyContinue
Copy-Item (Join-Path $localRoot '.env') (Join-Path $backupDir 'env.txt') -ErrorAction SilentlyContinue
if (Test-Path (Join-Path $localRoot 'reports')) {
    Copy-Item (Join-Path $localRoot 'reports') $backupDir -Recurse -ErrorAction SilentlyContinue
}

Get-ChildItem $backupDir | Select-Object Name, Length | Format-Table -AutoSize
Write-Host @'

恢复方法：
  PostgreSQL: docker cp <backup>\postgres-mailhub.dump mailhub-b4-postgres:/tmp/mailhub.dump
               docker exec mailhub-b4-postgres pg_restore -U mailhub -d mailhub --clean --if-exists /tmp/mailhub.dump
  宿主 SQLite: 停 host 后覆盖 data\local_host.db
'@
