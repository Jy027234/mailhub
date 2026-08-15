# Load the local-host/.env file into the current process environment.
# Skips comments and blank lines; existing environment wins.
param(
    [string]$EnvFile = (Join-Path $PSScriptRoot '..' '.env')
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $EnvFile)) {
    throw ".env not found at $EnvFile. Copy .env.example to .env and fill in secrets."
}

Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith('#')) { return }
    $separator = $line.IndexOf('=')
    if ($separator -le 0) { return }
    $name = $line.Substring(0, $separator).Trim()
    $value = $line.Substring($separator + 1).Trim()
    if (-not [string]::IsNullOrEmpty($value)) {
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
}
Write-Host "Loaded environment from $EnvFile"
