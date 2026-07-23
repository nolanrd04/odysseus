# Odysseus Stop Script (Windows, native - no Docker)
# Run this to stop Odysseus
# Run:  powershell -ExecutionPolicy Bypass -File .\stop-native.ps1

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$appPort = if ($env:APP_PORT) { $env:APP_PORT } else { "7700" }
$pidFile = Join-Path $PSScriptRoot "data\odysseus.pid"

Write-Host "Stopping Odysseus..."
$stopped = $false

if (Test-Path $pidFile) {
    $existingId = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($existingId -and (Get-Process -Id $existingId -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $existingId -Force -ErrorAction SilentlyContinue
        $stopped = $true
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

Get-NetTCPConnection -LocalPort $appPort -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object {
        Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
        $stopped = $true
    }

if ($stopped) {
    Write-Host "Odysseus has been stopped."
} else {
    Write-Host "Odysseus was not running."
}
