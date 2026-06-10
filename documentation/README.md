# Odysseus Developer Documentation

> Structured for LLM navigation. Each document covers a logical area of the codebase with links to related files. Start here to orient, then follow links to the relevant branch.

## What is Odysseus?

Odysseus is a self-hosted AI workspace built on **FastAPI**. It provides:
- Chat interface with streaming LLM responses
- Agent mode with 40+ tools (shell, Python, file ops, web search, email, calendar, memory)
- Deep research pipeline (iterative search → extract → synthesize)
- Document editor with AI editing
- Email inbox (IMAP/SMTP) with AI triage
- Calendar with CalDAV sync
- Image generation gallery
- Scheduled task automation (cron-style)
- Multi-model support (Ollama, OpenAI, Anthropic, Groq, OpenRouter, etc.)
- Vector memory with RAG (ChromaDB + fastembed)

Entry point: [`app.py`](/app.py) — FastAPI application with all routes registered.

---

## Documentation Tree

```
documentation/
├── README.md                  ← You are here
├── architecture.md            ← System architecture, data flow, key patterns
├── config/
│   └── README.md              ← Environment variables, deployment, Docker
├── core/
│   └── README.md              ← Auth, database ORM, middleware, session management
├── src/
│   ├── README.md              ← Business logic layer overview
│   ├── llm.md                 ← LLM client, streaming, providers, host management
│   ├── agent.md               ← Agent loop, tool execution, tool schemas
│   ├── research.md            ← Deep research pipeline, visual reports
│   ├── memory-rag.md          ← Embeddings, vector search, RAG
│   └── documents.md           ← Document processing, file uploads, OCR
├── routes/
│   ├── README.md              ← All API endpoints, route registration
│   ├── chat.md                ← Chat streaming and session endpoints
│   ├── email.md               ← Email inbox, polling, triage
│   ├── calendar.md            ← Calendar CRUD and CalDAV sync
│   ├── media.md               ← Image generation, TTS, STT
│   └── admin.md               ← Auth, users, settings, admin operations
├── services/
│   └── README.md              ← Service layer (memory, search, docs, cache, shell)
├── static/
│   └── README.md              ← Frontend SPA, JS module map, CSS
└── tests/
    └── README.md              ← Test suite layout and patterns
```

---

## Quick Navigation by Task

| Task | Primary files |
|------|---------------|
| Change how LLM calls are made | [`src/llm.md`](src/llm.md) → [`/src/llm_core.py`](/src/llm_core.py) |
| Add or modify an agent tool | [`src/agent.md`](src/agent.md) → [`/src/tool_implementations.py`](/src/tool_implementations.py), [`/src/tool_schemas.py`](/src/tool_schemas.py) |
| Add a new API endpoint | [`routes/README.md`](routes/README.md) → [`/app.py`](/app.py) |
| Modify authentication / sessions | [`core/README.md`](core/README.md) → [`/core/auth.py`](/core/auth.py) |
| Change database schema or ORM models | [`core/README.md`](core/README.md) → [`/core/database.py`](/core/database.py) |
| Add a new LLM provider | [`src/llm.md`](src/llm.md) → [`/src/integrations.py`](/src/integrations.py) |
| Modify the chat / streaming flow | [`routes/chat.md`](routes/chat.md) → [`/src/ai_interaction.py`](/src/ai_interaction.py) |
| Change vector memory or RAG | [`src/memory-rag.md`](src/memory-rag.md) → [`/src/rag_vector.py`](/src/rag_vector.py) |
| Modify deep research | [`src/research.md`](src/research.md) → [`/src/deep_research.py`](/src/deep_research.py) |
| Update email handling | [`routes/email.md`](routes/email.md) → [`/routes/email_routes.py`](/routes/email_routes.py) |
| Change document processing | [`src/documents.md`](src/documents.md) → [`/src/document_processor.py`](/src/document_processor.py) |
| Update environment / deployment config | [`config/README.md`](config/README.md) → [`/.env.example`](/.env.example) |
| Modify frontend UI | [`static/README.md`](static/README.md) → [`/static/js/`](/static/js/) |
| Run or write tests | [`tests/README.md`](tests/README.md) → [`/tests/`](/tests/) |
| Change scheduled tasks | [`src/agent.md`](src/agent.md) → [`/src/task_scheduler.py`](/src/task_scheduler.py) |
| Modify MCP server integration | [`src/agent.md`](src/agent.md) → [`/src/mcp_manager.py`](/src/mcp_manager.py), [`/mcp_servers/`](/mcp_servers/) |

---

## Runtime Data Paths (gitignored, created on first run)

| Path | Contents |
|------|----------|
| `data/app.db` | SQLite database — sessions, messages, tasks, users, calendar, tokens |
| `data/auth.json` | User credentials (bcrypt hashed) |
| `data/sessions.json` | Active session tokens |
| `data/settings.json` | App settings and provider configs |
| `data/memory.json` | Persistent memory state |
| `data/chroma/` | ChromaDB vector store |
| `data/uploads/` | User-uploaded files |
| `data/personal_docs/` | Indexed personal document library |
| `data/generated_images/` | Image generation output |
| `data/deep_research/` | Research reports |
| `logs/` | Application logs |

---

## Key Relationships

```
User request
  → FastAPI route (routes/)
    → Auth middleware (core/middleware.py, core/auth.py)
      → Business logic (src/)
        → LLM calls (src/llm_core.py)
        → Tool execution (src/tool_execution.py → src/tool_implementations.py)
        → Vector memory (services/memory/ + src/rag_vector.py)
        → Persistence (core/database.py → SQLite)
      → SSE stream back to frontend (static/)
```

See [`architecture.md`](architecture.md) for the full data flow diagram.
