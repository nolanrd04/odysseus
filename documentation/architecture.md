# System Architecture

> Parent: [`README.md`](README.md)

## Application Entry Point

**[`app.py`](/app.py)** — FastAPI application. Registers all routers from `routes/`, applies middleware, and mounts static files. Start here to see everything wired together.

## Layered Architecture

```
┌─────────────────────────────────────────────┐
│  Frontend (static/)                         │
│  SPA (index.html + app.js + static/js/)     │
│  PWA service worker, modular JS components  │
└──────────────────┬──────────────────────────┘
                   │ HTTP / SSE / WebSocket
┌──────────────────▼──────────────────────────┐
│  API Layer (routes/ — 50 route files)        │
│  FastAPI routers, request validation,        │
│  SSE streaming, file handling                │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│  Core Infrastructure (core/)                 │
│  Auth, session tokens, ORM models,           │
│  security middleware                         │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│  Business Logic (src/)                       │
│  LLM client, agent loop, tool execution,    │
│  research pipeline, document processing,    │
│  embeddings, background jobs                │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│  Service Layer (services/)                   │
│  ChromaDB client, SearXNG wrapper,          │
│  shell sandbox, TTS, YouTube, cache         │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│  Storage                                     │
│  SQLite (core/database.py)                  │
│  ChromaDB vector store (data/chroma/)       │
│  Filesystem (data/uploads/, data/docs/)     │
└─────────────────────────────────────────────┘
```

---

## Request Data Flow

### Standard Chat Request

```
1. POST /api/chat  (routes/chat_routes.py)
2. → Auth middleware validates session token (core/middleware.py → core/auth.py)
3. → chat_routes calls src/chat_handler.py
4. → chat_handler calls src/ai_interaction.py → stream_response()
5. → ai_interaction builds message context, calls src/llm_core.py → LLM API
6. → LLM response streamed as SSE back through route → frontend
7. → Messages persisted to SQLite (core/database.py)
```

### Agent Mode Request (tool use)

```
1. POST /api/agent  (routes/chat_routes.py or agent-specific route)
2. → src/agent_loop.py → run_agent_loop()
3. → Sends system prompt + tools to LLM (src/llm_core.py)
4. → LLM returns tool_calls in response
5. → src/tool_execution.py validates and dispatches each tool call
6. → src/tool_implementations.py executes the tool (shell, Python, web search, etc.)
7. → Tool result fed back to LLM as next message
8. → Loop repeats until LLM returns final text (no tool calls)
9. → Final response streamed to frontend
```

### Deep Research Request

```
1. POST /api/research  (routes/research_routes.py)
2. → src/research_handler.py orchestrates the session
3. → src/deep_research.py runs: think → search → extract → synthesize
4. → Each iteration: SearXNG search (services/search/) + page scraping
5. → Results synthesized by LLM into structured report
6. → src/visual_report.py renders Markdown → HTML
7. → Report saved to data/deep_research/
```

---

## Background Job System

Background jobs run independently of the request cycle. Coordination is through [`src/event_bus.py`](/src/event_bus.py).

| Job | File | Trigger |
|-----|------|---------|
| Email polling | `routes/email_pollers.py` | Timer / on-demand |
| Calendar sync | `src/caldav_sync.py` | Timer |
| Scheduled tasks (cron) | `src/task_scheduler.py` | Croniter schedule |
| Background job health | `src/bg_monitor.py` | Watchdog |
| General background work | `src/bg_jobs.py` | App startup |

---

## Authentication Flow

```
Request arrives
  → core/middleware.py checks for session cookie or Bearer token
  → core/auth.py validates against data/sessions.json + data/auth.json
  → If invalid → 401
  → If valid → request proceeds with user context injected
```

Multi-user: each user has their own data scope. Admin users can access all data.
2FA: via `pyotp` (TOTP), enforced in `core/auth.py`.

See [`core/README.md`](core/README.md).

---

## Key Cross-Cutting Patterns

### SSE Streaming
Routes that stream LLM output use `StreamingResponse` with `text/event-stream`. The frontend's `chatStream.js` consumes these. Never buffer a full response — always use async generators.

### Tool Registration
Tools are registered in two places that must stay in sync:
1. **Schema** — `src/tool_schemas.py` (OpenAI function call format)
2. **Implementation** — `src/tool_implementations.py`

Adding a tool requires updating both files. See [`src/agent.md`](src/agent.md).

### MCP Servers
External and built-in MCP servers are managed by `src/mcp_manager.py`. Built-in servers live in `mcp_servers/`. The MCP protocol is used for tool discovery and execution by external tool hosts.

### Configuration
All runtime config flows through `src/config.py` which reads from `.env` / environment variables. Constants that don't vary per environment live in `core/constants.py`. See [`config/README.md`](config/README.md).

### Vector Memory
Two paths to vector search:
1. **Local** — fastembed (ONNX, `src/embeddings.py`) + ChromaDB (`services/memory/`)
2. **Remote** — External embedding API (configured via `EMBEDDING_URL`)

`src/rag_vector.py` unifies both with keyword fallback. See [`src/memory-rag.md`](src/memory-rag.md).

---

## Directory Map

| Directory | Role | Docs |
|-----------|------|------|
| `core/` | Auth, DB, middleware | [`core/README.md`](core/README.md) |
| `src/` | Business logic | [`src/README.md`](src/README.md) |
| `routes/` | FastAPI endpoints (50 files) | [`routes/README.md`](routes/README.md) |
| `services/` | Service clients and wrappers | [`services/README.md`](services/README.md) |
| `static/` | Frontend SPA | [`static/README.md`](static/README.md) |
| `mcp_servers/` | Built-in MCP server implementations | [`src/agent.md`](src/agent.md) |
| `companion/` | Device pairing for remote access | — |
| `tests/` | 200+ test files | [`tests/README.md`](tests/README.md) |
| `docker/` | GPU overlay compose files | [`config/README.md`](config/README.md) |
| `scripts/` | GPU diagnostics, utilities | — |
| `docs/` | Landing page and demo media | — |
