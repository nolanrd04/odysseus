# Configuration & Deployment

> Parent: [`../README.md`](../README.md) | Architecture: [`../architecture.md`](../architecture.md)

---

## Environment Variables

All configuration is via environment variables, read by [`src/config.py`](/src/config.py). The canonical reference is [`.env.example`](/.env.example) — copy it to `.env` and edit.

### Core App

| Variable | Default | Purpose |
|----------|---------|---------|
| `APP_BIND` | `127.0.0.1` | Network interface to listen on |
| `APP_PORT` | `7000` | Port for the web UI |
| `SECURE_COOKIES` | `false` | Set `true` behind HTTPS reverse proxy |
| `REQUEST_HARD_TIMEOUT` | `45` | Request timeout in seconds (excluding SSE) |

### Authentication

| Variable | Default | Purpose |
|----------|---------|---------|
| `AUTH_ENABLED` | `true` | Enable login system |
| `LOCALHOST_BYPASS` | `false` | Skip auth for localhost requests (dev only) |

### LLM

| Variable | Default | Purpose |
|----------|---------|---------|
| `LLM_HOST` | `http://localhost:11434` | Primary LLM server (Ollama default) |
| `LLM_HOSTS` | — | Comma-separated pool of LLM hosts |
| `DEFAULT_MODEL` | — | Default model name |
| `OPENAI_API_KEY` | — | OpenAI API key |
| `ANTHROPIC_API_KEY` | — | Anthropic API key |
| `GROQ_API_KEY` | — | Groq API key |
| `OPENROUTER_API_KEY` | — | OpenRouter API key |

### Embeddings & Vector Memory

| Variable | Default | Purpose |
|----------|---------|---------|
| `EMBEDDING_URL` | — | Remote embedding API base URL (optional) |
| `EMBEDDING_MODEL` | — | Model name for remote embedding API |
| `FASTEMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Local ONNX embedding model |
| `CHROMADB_HOST` | `localhost` | ChromaDB host |
| `CHROMADB_PORT` | `8000` | ChromaDB port |

### Search

| Variable | Default | Purpose |
|----------|---------|---------|
| `SEARXNG_INSTANCE` | `http://localhost:8080` | SearXNG base URL |
| `DATA_BRAVE_API_KEY` | — | Brave Search API key |
| `TAVILY_API_KEY` | — | Tavily search API key |
| `SERPER_API_KEY` | — | Serper search API key |

### Database

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | `sqlite:///./data/app.db` | SQLAlchemy connection string |

### Docker

| Variable | Default | Purpose |
|----------|---------|---------|
| `PUID` | `1000` | User ID for file ownership in container |
| `PGID` | `1000` | Group ID for file ownership in container |

---

## Deployment Options

### Docker (recommended)

```bash
# Standard CPU
./build.sh && ./start.sh

# With NVIDIA GPU
docker compose -f docker-compose.yml -f docker/gpu.nvidia.yml up -d

# With AMD GPU
docker compose -f docker-compose.yml -f docker/gpu.amd.yml up -d

# Stop
./stop.sh
```

**Services started by `docker-compose.yml`:**
- `odysseus` — main app (this repo)
- `chromadb` — vector database
- `searxng` — web search engine
- `ntfy` — push notifications (optional)

### Native macOS

```bash
./start-macos.sh
# Starts on port 7860 by default (configurable)
```

### Native Linux (systemd)

```bash
./install-service.sh
systemctl start odysseus-ui
```

### Windows

```powershell
.\launch-windows.ps1
```

### macOS Desktop App

```bash
./build-macos-app.sh
# Creates a .app bundle
```

---

## First-Run Setup

[`setup.py`](/setup.py) runs automatically on first start (or can be run manually):
- Creates `data/` directory structure
- Initializes the SQLite database schema
- Generates initial admin account credentials
- Writes default `data/settings.json`

---

## Key Config Files

| File | Purpose |
|------|---------|
| `.env.example` | Template — copy to `.env` and edit |
| `.env` | Active config (gitignored) |
| `docker-compose.yml` | Docker service definitions |
| `docker/gpu.nvidia.yml` | NVIDIA GPU overlay for docker compose |
| `docker/gpu.amd.yml` | AMD GPU overlay for docker compose |
| `Dockerfile` | Python 3.12 slim image build |
| `odysseus-ui.service` | systemd unit file |
| `pyproject.toml` | Pytest config |
| `requirements.txt` | Python dependencies |
| `requirements-optional.txt` | Optional deps (GPU-specific, extra providers) |

---

## GPU Diagnostics

```bash
# NVIDIA
./scripts/check-docker-gpu.sh

# AMD
./scripts/check-docker-amd-gpu.sh
```

---

## Reverse Proxy Notes

When running behind nginx/Caddy/Traefik:
- Set `SECURE_COOKIES=true` so session cookies have the `Secure` flag
- Proxy `/api/chat/*` and `/api/research/*` with buffering disabled (SSE requires `proxy_buffering off` in nginx)
- Set `APP_BIND=0.0.0.0` or the internal container IP

---

## Related

- Runtime config source → [`/src/config.py`](/src/config.py)
- App constants → [`/core/constants.py`](/core/constants.py)
- Auth config → [`../core/README.md`](../core/README.md)
