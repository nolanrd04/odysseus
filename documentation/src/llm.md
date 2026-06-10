# LLM Client, Providers & Streaming (`src/`)

> Parent: [`README.md`](README.md) | Architecture: [`../architecture.md`](../architecture.md)

---

## Core Files

### [`src/llm_core.py`](/src/llm_core.py) (~1479 lines)
The central LLM API client. All LLM calls in the application go through here.

**Key responsibilities:**
- Sends chat completion requests to any OpenAI-compatible endpoint
- Handles streaming responses (async generator → SSE)
- Response caching (avoids duplicate calls for identical prompts)
- **Host health tracking** — tracks which LLM hosts are responding; backs off dead hosts with a cooldown timer
- Retry logic with exponential backoff
- Token counting and context window management

**Key functions:**
- `stream_completion(messages, model, tools, ...)` — main streaming entry point
- `complete(messages, model, ...)` — non-streaming completion
- `get_healthy_host()` — returns a live LLM host from the configured pool
- `mark_host_dead(host)` / `mark_host_alive(host)` — used by callers to report host state

**Multi-host support:** Set `LLM_HOSTS` (comma-separated) to enable host pool. Requests are distributed across healthy hosts with automatic failover.

---

### [`src/ai_interaction.py`](/src/ai_interaction.py) (~1807 lines)
Conversation-level logic that sits on top of `llm_core.py`.

**Key responsibilities:**
- Builds the full message list (system prompt + history + new user message)
- Context window management — trims oldest messages when token limit approached
- Injects RAG context (vector memory results) into system prompt
- Handles image attachments in messages (vision-capable models)
- Streams final response back to the caller

**Key functions:**
- `stream_response(session_id, user_message, ...)` — main entry for chat route
- `build_context(session_id, ...)` — constructs message array from DB history
- `inject_rag_context(system_prompt, query)` — pulls relevant memories via [`src/rag_vector.py`](/src/rag_vector.py)

---

### [`src/chat_handler.py`](/src/chat_handler.py)
Thin layer between the chat route and `ai_interaction.py`. Handles SSE event formatting and error wrapping.

---

### [`src/chat_processor.py`](/src/chat_processor.py)
Pre-processes incoming messages before they hit `ai_interaction.py`. Handles:
- Context window trimming strategy
- Token estimation per message
- Multimodal message normalization (text + images)

---

### [`src/integrations.py`](/src/integrations.py) (~492 lines)
Provider configuration registry. Maps provider names to their API base URLs, authentication schemes, and model lists.

**Providers supported:**
- Ollama (local, default)
- OpenAI
- Anthropic
- Groq
- OpenRouter
- Mistral
- Cohere
- Custom OpenAI-compatible endpoints

**To add a new provider:** Add an entry to the provider map in this file with its base URL and auth scheme. `llm_core.py` will pick it up automatically.

---

## Configuration

| Env Var | Purpose | Default |
|---------|---------|---------|
| `LLM_HOST` | Primary LLM server host | `http://localhost:11434` |
| `LLM_HOSTS` | Comma-separated host pool | (single host) |
| `OPENAI_API_KEY` | OpenAI API key (optional) | — |
| `ANTHROPIC_API_KEY` | Anthropic API key (optional) | — |
| `REQUEST_HARD_TIMEOUT` | Request timeout in seconds | `45` |
| `DEFAULT_MODEL` | Default model name | — |

See [`../config/README.md`](../config/README.md) for the full list.

---

## Data Flow

```
chat_routes.py
  → chat_handler.py
    → ai_interaction.py   (builds context, injects RAG)
      → rag_vector.py     (retrieves relevant memories)
      → llm_core.py       (sends to LLM, streams response)
        → integrations.py (resolves provider endpoint)
```

---

## Related

- Tools (agent mode) also call `llm_core.py` → see [`agent.md`](agent.md)
- Research pipeline uses `llm_core.py` for synthesis → see [`research.md`](research.md)
- RAG injection → see [`memory-rag.md`](memory-rag.md)
- Chat routes → see [`../routes/chat.md`](../routes/chat.md)
