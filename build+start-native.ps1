# Odysseus Build + Start Script (Windows, native - no Docker)
# Stops any running Odysseus process, updates the virtualenv, then starts it
# locally or on your tailnet. The server runs detached (like `docker compose
# up -d` did) - closing this window will NOT stop it. Use stop-native.ps1
# to stop it.
# Run:  powershell -ExecutionPolicy Bypass -File .\build+start-native.ps1

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Write-Step($msg) { Write-Host ""; Write-Host ("==> " + $msg) -ForegroundColor Cyan }
function Fail($msg) {
    Write-Host ""
    Write-Host ("ERROR: " + $msg) -ForegroundColor Red
    exit 1
}

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

$appPort = $env:APP_PORT
$appBind = $env:APP_BIND

$dataDir = Join-Path $PSScriptRoot "data"
$logsDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

$pidFile = Join-Path $dataDir "odysseus.pid"
$outLog  = Join-Path $logsDir "odysseus.out.log"
$errLog  = Join-Path $logsDir "odysseus.err.log"

# --- Stop any existing instance ---------------------------------------------
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

# Fallback: kill whatever else is already bound to the port (stale process,
# no pid file, started some other way, etc.) - mirrors `docker compose down`
# guaranteeing a clean slate.
Get-NetTCPConnection -LocalPort $appPort -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object {
        Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
        $stopped = $true
    }

if ($stopped) {
    Start-Sleep -Seconds 1
    Write-Host "Odysseus has been stopped."
} else {
    Write-Host "Odysseus was not running."
}

# --- Ensure Python + venv exist ---------------------------------------------
Write-Step "Checking for Python"
function Get-PythonVersionText($launcher, $launcherArgs) {
    try {
        return (& $launcher @launcherArgs -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null).Trim()
    } catch { return $null }
}

$pyExe = $null
$pyArgs = @()

$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($pyLauncher) {
    foreach ($v in @("-3.13", "-3.12", "-3.11")) {
        $ver = Get-PythonVersionText $pyLauncher.Source @($v)
        if ($ver) { $pyExe = $pyLauncher.Source; $pyArgs = @($v); break }
    }
}
if (-not $pyExe) {
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) {
        $ver = Get-PythonVersionText $pythonCmd.Source @()
        if ($ver) {
            $parts = $ver.Split('.')
            if ([int]$parts[0] -gt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 11)) {
                $pyExe = $pythonCmd.Source
            }
        }
    }
}
if (-not $pyExe) {
    Fail "Couldn't find Python 3.11+. Install it from https://www.python.org/downloads/ and re-run this script."
}

$venvPy = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Step "Creating virtual environment (venv)"
    & $pyExe @pyArgs -m venv venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) { Fail "Failed to create the virtual environment." }
}

# --- "Build": install/update dependencies -----------------------------------
Write-Step "Installing dependencies"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "Dependency install failed. Scroll up for the pip error." }

Write-Step "Running first-time setup"
& $venvPy setup.py
if ($LASTEXITCODE -ne 0) { Fail "setup.py failed." }

# --- Start --------------------------------------------------------------------
Write-Host "Starting Odysseus..."

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

# Truncate old logs so the tail below starts clean
Remove-Item $outLog, $errLog -ErrorAction SilentlyContinue

# Force UTF-8 for the whole interpreter (PEP 540). Without this, stdout/stderr
# and any file I/O without an explicit encoding= fall back to the OS ANSI
# codepage on Windows — fine on machines with the "Beta: Unicode UTF-8"
# region setting enabled, but a Windows Server install typically doesn't have
# that, so any print()/write of a non-ASCII character (≥, →, em-dashes, etc.
# all appear in the QP knowledge-pack derivation output) crashes with
# "'charmap' codec can't encode character ...".
$env:PYTHONUTF8 = "1"

# Force unbuffered stdout/stderr. Since RedirectStandardOutput/Error below
# points at a file rather than a real console, Python fully block-buffers
# those streams by default — plain print() calls (e.g. the QP knowledge-pack
# derivation trace) can sit invisible for a long stretch before actually
# landing in the log file, even though logging-module output (uvicorn access
# logs, logger.info calls) auto-flushes per line and appears immediately.
$env:PYTHONUNBUFFERED = "1"

$proc = Start-Process -FilePath $venvPy `
    -ArgumentList @("-m", "uvicorn", "app:app", "--host", $appBind, "--port", $appPort) `
    -WorkingDirectory $PSScriptRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -PassThru
$proc.Id | Out-File -FilePath $pidFile -Encoding ascii -NoNewline

# Wait for containers... er, the process, to be healthy
Write-Host "Waiting for Odysseus to start..."
Start-Sleep -Seconds 5

# Total budget: 5s initial sleep + HEALTH_CHECK_ATTEMPTS * HEALTH_CHECK_INTERVAL seconds.
Set-EnvDefault HEALTH_CHECK_ATTEMPTS "45"
Set-EnvDefault HEALTH_CHECK_INTERVAL "2"
$attempts = [int]$env:HEALTH_CHECK_ATTEMPTS
$interval = [int]$env:HEALTH_CHECK_INTERVAL

for ($i = 1; $i -le $attempts; $i++) {
    if ($proc.HasExited) {
        Fail "Odysseus process exited unexpectedly. Check '$errLog' for errors."
    }

    $healthy = $false
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:$appPort/api/health" -UseBasicParsing -TimeoutSec 5 | Out-Null
        $healthy = $true
    } catch { }

    if ($healthy) {
        Write-Host "Odysseus is running! (PID $($proc.Id))"
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
        Write-Host "Odysseus keeps running after you close this window."
        Write-Host "Run 'stop-native' script when done. Tailing logs below (Ctrl+C just stops watching):"
        Write-Host ""

        # Tail logs, hiding successful (HTTP 200) request lines. Ctrl+C to stop
        # watching - the server itself is a separate detached process.
        Get-Content -Path $outLog, $errLog -Wait -Tail 0 |
            Where-Object { $_ -notmatch '" 200 ' }
        exit 0
    }
    if ($i % 5 -eq 0) {
        Write-Host "   Still waiting... ($($i * $interval + 5)s elapsed)"
    }
    Start-Sleep -Seconds $interval
}

Write-Host "Odysseus failed to start after $($attempts * $interval + 5)s. Check '$errLog' for errors"
exit 1
