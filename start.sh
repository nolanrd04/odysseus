#!/bin/zsh
# Odysseus Start Script
# Run this to start Odysseus locally or on your tailnet

set -e

echo "🚀 Starting Odysseus..."

# Set default bind address and port for tailnet access
export APP_BIND="${APP_BIND:-0.0.0.0}"
export APP_PORT="${APP_PORT:-7700}"
export LLM_HOST="${LLM_HOST:-localhost}"
export LLM_HOSTS="${LLM_HOSTS:-}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-}"
export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
export RESEARCH_LLM_ENDPOINT="${RESEARCH_LLM_ENDPOINT:-}"
export HF_TOKEN="${HF_TOKEN:-}"
export HUGGING_FACE_HUB_TOKEN="${HUGGING_FACE_HUB_TOKEN:-}"
export AUTH_ENABLED="${AUTH_ENABLED:-true}"
export LOCALHOST_BYPASS="${LOCALHOST_BYPASS:-false}"
export ALLOWED_ORIGINS="${ALLOWED_ORIGINS:-http://localhost,http://127.0.0.1}"
export SECURE_COOKIES="${SECURE_COOKIES:-false}"
export EMBEDDING_URL="${EMBEDDING_URL:-}"
export EMBEDDING_MODEL="${EMBEDDING_MODEL:-}"
export FASTEMBED_MODEL="${FASTEMBED_MODEL:-sentence-transformers/all-MiniLM-L6-v2}"
export FASTEMBED_CACHE_PATH="${FASTEMBED_CACHE_PATH:-}"
export CLEANUP_INTERVAL_HOURS="${CLEANUP_INTERVAL_HOURS:-24}"
export ODYSSEUS_INPROCESS_POLLERS="${ODYSSEUS_INPROCESS_TASKS:-1}"
export ODYSSEUS_INPROCESS_TASKS="${ODYSSEUS_INPROCESS_TASKS:-1}"
export ODYSSEUS_SCRIPT_HOST="${ODYSSEUS_SCRIPT_HOST:-localhost}"
export DATA_BRAVE_API_KEY="${DATA_BRAVE_API_KEY:-}"
export GOOGLE_API_KEY="${GOOGLE_API_KEY:-}"
export GOOGLE_PSE_CX="${GOOGLE_PSE_CX:-}"
export TAVILY_API_KEY="${TAVILY_API_KEY:-}"
export SERPER_API_KEY="${SERPER_API_KEY:-}"
export NTFY_BASE_URL="${NTFY_BASE_URL:-http://localhost:8091}"
export PUID="${PUID:-1000}"
export PGID="${PGID:-1000}"

# Get your Tailscale IP if available
if command -v tailscale &>/dev/null && tailscale status --json &>/dev/null | grep -q '"peers"'; then
    TAILSCALE_IP=$(tailscale ip)
    echo "📍 Your Tailscale IP: $TAILSCALE_IP"
    echo "📱 Access from any tailnet device at: http://$TAILSCALE_IP:$APP_PORT"
else
    echo "ℹ️  Not on Tailscale or not connected. Use direct IP if needed."
fi

# Start Docker containers in detached mode
docker compose up -d

# Wait for containers to be healthy
echo "⏳ Waiting for Odysseus to start..."
sleep 5

# Check if Odysseus is responding
for i in {1..10}; do
    if curl -s "http://127.0.0.1:$APP_PORT/api/health" &>/dev/null; then
        echo "✅ Odysseus is running!"
        echo ""
        echo "📍 Access URLs:"
        echo "   Local:   http://127.0.0.1:$APP_PORT"
        echo "   Network: http://$(hostname -I | awk '{print $1}'):$APP_PORT"
        if [ -n "$TAILSCALE_IP" ]; then
            echo "   Tailnet: http://$TAILSCALE_IP:$APP_PORT"
        fi
        echo ""
        echo "📖 Run 'stop.sh' when done"
        exit 0
    fi
    echo "   Waiting... ($i/10)"
    sleep 2
done

echo "❌ Odysseus failed to start. Check 'docker logs odysseus-odysseus-1' for errors"
exit 1