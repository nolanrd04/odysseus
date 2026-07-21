# Odysseus Build + Start Script (Windows)
# Stops Odysseus, rebuilds the images, then starts it locally or on your tailnet.
# Run:  powershell -ExecutionPolicy Bypass -File .\build+start.ps1

$ErrorActionPreference = "Stop"

Write-Host "Stopping Odysseus..."
docker compose down
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Odysseus has been stopped."

docker compose build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Starting Odysseus..."

# Set defaults only when the variable isn't already set in the environment
function Set-EnvDefault([string]$Name, [string]$Default) {
    if (-not (Test-Path "Env:$Name") -or [string]::IsNullOrEmpty((Get-Item "Env:$Name").Value)) {
        Set-Item "Env:$Name" $Default
    }
}

Set-EnvDefault APP_BIND "0.0.0.0"
Set-EnvDefault APP_PORT "7700"
Set-EnvDefault LLM_HOST "localhost"
Set-EnvDefault LLM_HOSTS ""
Set-EnvDefault OPENAI_API_KEY ""
Set-EnvDefault OLLAMA_BASE_URL "http://localhost:11434"
Set-EnvDefault RESEARCH_LLM_ENDPOINT ""
Set-EnvDefault HF_TOKEN ""
Set-EnvDefault HUGGING_FACE_HUB_TOKEN ""
Set-EnvDefault AUTH_ENABLED "true"
Set-EnvDefault LOCALHOST_BYPASS "false"
Set-EnvDefault ALLOWED_ORIGINS "http://localhost,http://127.0.0.1"
Set-EnvDefault SECURE_COOKIES "false"
Set-EnvDefault EMBEDDING_URL ""
Set-EnvDefault EMBEDDING_MODEL ""
Set-EnvDefault FASTEMBED_MODEL "sentence-transformers/all-MiniLM-L6-v2"
Set-EnvDefault FASTEMBED_CACHE_PATH ""
Set-EnvDefault CLEANUP_INTERVAL_HOURS "24"
Set-EnvDefault ODYSSEUS_INPROCESS_POLLERS "1"
Set-EnvDefault ODYSSEUS_INPROCESS_TASKS "1"
Set-EnvDefault ODYSSEUS_SCRIPT_HOST "localhost"
Set-EnvDefault DATA_BRAVE_API_KEY ""
Set-EnvDefault GOOGLE_API_KEY ""
Set-EnvDefault GOOGLE_PSE_CX ""
Set-EnvDefault TAVILY_API_KEY ""
Set-EnvDefault SERPER_API_KEY ""
Set-EnvDefault NTFY_BASE_URL "http://localhost:8091"
Set-EnvDefault PUID "1000"
Set-EnvDefault PGID "1000"

$appPort = $env:APP_PORT

# Get your Tailscale IP if available
$tailscaleIp = $null
if (Get-Command tailscale -ErrorAction SilentlyContinue) {
    try {
        $tailscaleIp = (& tailscale ip -4 2>$null | Select-Object -First 1)
        if ($LASTEXITCODE -ne 0) { $tailscaleIp = $null }
    } catch { $tailscaleIp = $null }
}
if ($tailscaleIp) {
    Write-Host "Your Tailscale IP: $tailscaleIp"
    Write-Host "Access from any tailnet device at: http://${tailscaleIp}:$appPort"
} else {
    Write-Host "Not on Tailscale or not connected. Use direct IP if needed."
}

# Start Docker containers in detached mode
docker compose up -d
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# Wait for containers to be healthy
Write-Host "Waiting for Odysseus to start..."
Start-Sleep -Seconds 5

# Total budget: 5s initial sleep + HEALTH_CHECK_ATTEMPTS * HEALTH_CHECK_INTERVAL seconds.
Set-EnvDefault HEALTH_CHECK_ATTEMPTS "45"
Set-EnvDefault HEALTH_CHECK_INTERVAL "2"
$attempts = [int]$env:HEALTH_CHECK_ATTEMPTS
$interval = [int]$env:HEALTH_CHECK_INTERVAL

for ($i = 1; $i -le $attempts; $i++) {
    $healthy = $false
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:$appPort/api/health" -UseBasicParsing -TimeoutSec 5 | Out-Null
        $healthy = $true
    } catch { }

    if ($healthy) {
        Write-Host "Odysseus is running!"
        Write-Host ""
        Write-Host "Access URLs:"
        Write-Host "   Local:   http://127.0.0.1:$appPort"
        $lanIp = (Get-NetIPAddress -AddressFamily IPv4 |
            Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
            Select-Object -First 1).IPAddress
        if ($lanIp) {
            Write-Host "   Network: http://${lanIp}:$appPort"
        }
        if ($tailscaleIp) {
            Write-Host "   Tailnet: http://${tailscaleIp}:$appPort"
        }
        Write-Host ""
        Write-Host "Run 'stop' script when done"

        # Tail logs, hiding successful (HTTP 200) request lines. Ctrl+C to stop.
        # Merge stderr inside cmd.exe: docker logs emits to stderr, which
        # PowerShell 5.1 would otherwise turn into NativeCommandError records.
        cmd /c "docker logs -f odysseus-odysseus-1 2>&1" |
            Where-Object { $_ -notmatch '" 200 ' }
        exit 0
    }
    if ($i % 5 -eq 0) {
        Write-Host "   Still waiting... ($($i * $interval + 5)s elapsed)"
    }
    Start-Sleep -Seconds $interval
}

Write-Host "Odysseus failed to start after $($attempts * $interval + 5)s. Check 'docker logs odysseus-odysseus-1' for errors"
exit 1