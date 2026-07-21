# Odysseus Stop Script (Windows)
# Run this to stop Odysseus
# Run:  powershell -ExecutionPolicy Bypass -File .\stop.ps1

$ErrorActionPreference = "Stop"

Write-Host "Stopping Odysseus..."

# Stop all containers
docker compose down
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Odysseus has been stopped."
