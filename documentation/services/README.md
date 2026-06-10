# Service Layer (`services/`)

> Parent: [`../README.md`](../README.md) | Architecture: [`../architecture.md`](../architecture.md)

The `services/` directory contains client wrappers and provider abstractions used by `src/` modules. Each service encapsulates a specific external dependency so the business logic doesn't depend directly on third-party APIs.

---

## Directories

### `services/memory/`
ChromaDB client wrapper and vector collection management.

**Used by:** `src/rag_vector.py`

**What it provides:**
- `MemoryService` class — connects to ChromaDB (local or Docker container)
- Collection CRUD (create, get, delete)
- Upsert and query operations
- Multi-tenant collection namespacing (per user)

ChromaDB connection is configured via `CHROMADB_HOST` / `CHROMADB_PORT` env vars. In Docker, ChromaDB runs as a separate container; locally, it defaults to localhost.

---

### `services/search/`
Web search provider wrapper.

**Used by:** `src/deep_research.py`, `src/tool_implementations.py` (web_search tool)

**What it provides:**
- `SearchService` — unified interface over multiple search backends
- Provider priority: SearXNG → Brave → Tavily → Serper
- Returns structured results: title, URL, snippet

**SearXNG:** the default search backend, runs as a Docker container at `SEARXNG_INSTANCE`. For local (non-Docker) setups, configure `SEARXNG_INSTANCE` to point to your SearXNG instance.

---

### `services/docs/`
Document indexing and retrieval service.

**Used by:** `routes/document_routes.py`, `src/rag_vector.py`

**What it provides:**
- Index management for the `documents` ChromaDB collection
- Document chunking strategies (by paragraph, by token count)
- Metadata management (title, source, date)

---

### `services/shell/`
Shell command execution sandbox.

**Used by:** `src/tool_implementations.py` (run_shell_command tool), `routes/shell_routes.py`

**What it provides:**
- `ShellService` — executes shell commands with:
  - Working directory confinement
  - Timeout enforcement
  - Output capture (stdout + stderr)
  - Optional SSH execution (for remote shell)

---

### `services/tts/`
Text-to-speech provider wrappers.

**Used by:** `routes/tts_routes.py`

**Providers:**
- `OpenAITTS` — OpenAI TTS API
- `ElevenLabsTTS` — ElevenLabs API
- `LocalTTS` — system/bundled TTS

All implement a common `synthesize(text, voice) -> audio_bytes` interface.

---

### `services/youtube/`
YouTube transcript fetching.

**Used by:** `src/tool_implementations.py` (fetch YouTube transcripts as tool)

**What it provides:**
- `get_transcript(video_url) -> str` — fetches auto-generated or manual subtitles via `youtube-transcript-api`

---

### `services/cache/`
Multi-layer caching service.

**Used by:** `src/llm_core.py` (response caching), other modules

**What it provides:**
- In-memory LRU cache
- Optional Redis backend (if configured)
- TTL-based expiry

---

### `services/hwfit/`
Hardware fitting — model recommendations based on system specs.

**Used by:** `routes/hwfit_routes.py`, `routes/cookbook_routes.py`

**What it provides:**
- GPU VRAM detection
- System RAM detection
- Model quantization compatibility scoring
- Wraps the `llmfit` library

---

### `services/research/`
Research report storage and retrieval.

**Used by:** `src/research_handler.py`

**What it provides:**
- Save/load research sessions and reports from `data/deep_research/`
- Report metadata indexing

---

## Pattern

Each service follows a class-based pattern:
```python
class SearchService:
    def __init__(self, config): ...
    def search(self, query: str) -> list[dict]: ...
```

Services are instantiated at app startup (in `app.py` or via FastAPI's lifespan) and injected as dependencies or accessed via module-level singletons.

---

## Related

- Vector memory details → [`../src/memory-rag.md`](../src/memory-rag.md)
- Research pipeline → [`../src/research.md`](../src/research.md)
- Agent tools → [`../src/agent.md`](../src/agent.md)
- Config → [`../config/README.md`](../config/README.md)
