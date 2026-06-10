# Business Logic Layer (`src/`)

> Parent: [`../README.md`](../README.md) | Architecture: [`../architecture.md`](../architecture.md)

The `src/` directory contains all business logic — everything between the API route handlers and the storage layer. This is where LLM calls, agent execution, research, document processing, and background jobs live.

---

## Sub-documents

| Topic | File | Key source files |
|-------|------|-----------------|
| LLM client, providers, streaming | [`llm.md`](llm.md) | `llm_core.py`, `integrations.py`, `ai_interaction.py` |
| Agent loop, tools, MCP | [`agent.md`](agent.md) | `agent_loop.py`, `tool_implementations.py`, `tool_schemas.py`, `tool_execution.py`, `task_scheduler.py` |
| Deep research pipeline | [`research.md`](research.md) | `deep_research.py`, `research_handler.py`, `visual_report.py` |
| Vector memory and RAG | [`memory-rag.md`](memory-rag.md) | `embeddings.py`, `rag_vector.py` |
| Document processing and uploads | [`documents.md`](documents.md) | `document_processor.py`, `upload_handler.py` |

---

## All Files at a Glance

### LLM & Providers
| File | Purpose |
|------|---------|
| `src/llm_core.py` | LLM API client: streaming, caching, host health, retry logic, OpenAI-compatible |
| `src/integrations.py` | Provider configs: OpenAI, Anthropic, Groq, OpenRouter, Ollama, etc. |
| `src/ai_interaction.py` | Conversation management: context window, message history, response streaming |
| `src/chat_handler.py` | Chat streaming and event handling (called by chat routes) |
| `src/chat_processor.py` | Message preprocessing, context window trimming |

### Agent & Tools
| File | Purpose |
|------|---------|
| `src/agent_loop.py` | Multi-round agent orchestration; tool call parsing and re-submission |
| `src/tool_implementations.py` | Implementations of all 40+ tools |
| `src/tool_schemas.py` | OpenAI function-call JSON schemas for all tools |
| `src/tool_execution.py` | Tool safety, sandboxing, timeout enforcement |
| `src/mcp_manager.py` | MCP server lifecycle: discovery, registration, tool proxying |
| `src/builtin_mcp.py` | Auto-registers built-in MCP servers (e.g., Browser via Playwright) |
| `src/task_scheduler.py` | Cron-style scheduled tasks; agent-driven automation |
| `src/builtin_actions.py` | Higher-level built-in actions: document editing, image gen, email triage, calendar sync |

### Research
| File | Purpose |
|------|---------|
| `src/deep_research.py` | Iterative research: think → search → extract → synthesize |
| `src/research_handler.py` | Research session orchestration and state |
| `src/visual_report.py` | Markdown → HTML report rendering |
| `src/goal_based_extractor.py` | Extracts key insights from raw research results |

### Memory & RAG
| File | Purpose |
|------|---------|
| `src/embeddings.py` | Vector embeddings: local fastembed (ONNX) or remote API |
| `src/rag_vector.py` | ChromaDB-backed RAG with keyword fallback |

### Documents & Uploads
| File | Purpose |
|------|---------|
| `src/document_processor.py` | Multi-format parsing: PDF, HTML, CSV, Markdown, Office (via markitdown) |
| `src/upload_handler.py` | File uploads: type detection, vision model integration, OCR |

### Email
| File | Purpose |
|------|---------|
| `src/email_thread_parser.py` | Reconstructs email threads from IMAP messages |

### Calendar
| File | Purpose |
|------|---------|
| `src/caldav_sync.py` | CalDAV sync: Apple Calendar, Fastmail, Nextcloud, Radicale |

### Infrastructure
| File | Purpose |
|------|---------|
| `src/config.py` | Runtime configuration from env vars |
| `src/event_bus.py` | In-process pub/sub for background job coordination |
| `src/bg_jobs.py` | Background task runner (email polling, calendar sync, etc.) |
| `src/bg_monitor.py` | Background job health watchdog |
| `src/teacher_escalation.py` | Educational content generation for assistant learning mode |

---

## Configuration Entry Point

`src/config.py` is the single source of truth for runtime configuration. It reads from environment variables and `.env`. All other modules should import config from here rather than reading env vars directly.

See [`../config/README.md`](../config/README.md) for all configurable variables.
