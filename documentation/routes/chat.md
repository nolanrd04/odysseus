# Chat & Session Routes

> Parent: [`README.md`](README.md) | Architecture: [`../architecture.md`](../architecture.md)

---

## Files

### [`routes/chat_routes.py`](/routes/chat_routes.py) (~1288 lines)
Primary chat endpoint — handles message submission and LLM response streaming.

**Key endpoints:**
| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/chat` | Submit a message, receive streamed LLM response (SSE) |
| `POST` | `/api/chat/agent` | Submit to agent mode (tool use loop) |
| `GET` | `/api/chat/history/{session_id}` | Load conversation history |
| `POST` | `/api/chat/rag` | Direct RAG query (search memories/docs) |
| `DELETE` | `/api/chat/{message_id}` | Delete a message |
| `PUT` | `/api/chat/{message_id}` | Edit a message |

**Streaming:** responses use `StreamingResponse` with `text/event-stream`. Each SSE event is a JSON chunk:
```
data: {"type": "token", "content": "Hello"}
data: {"type": "done"}
data: {"type": "error", "message": "..."}
```

**Call chain for standard chat:**
```
POST /api/chat
  → chat_routes.py
    → src/chat_handler.py
      → src/ai_interaction.py → stream_response()
        → src/rag_vector.py (context injection)
        → src/llm_core.py (LLM API call)
```

**Call chain for agent mode:**
```
POST /api/chat/agent
  → chat_routes.py
    → src/agent_loop.py → run_agent_loop()
      → src/tool_execution.py → src/tool_implementations.py
      → src/llm_core.py (repeated per tool round)
```

---

### [`routes/session_routes.py`](/routes/session_routes.py) (~1181 lines)
CRUD operations for chat sessions (conversations).

**Key endpoints:**
| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/sessions` | List all sessions for current user |
| `POST` | `/api/sessions` | Create new session |
| `GET` | `/api/sessions/{id}` | Get session metadata |
| `PUT` | `/api/sessions/{id}` | Rename or update session |
| `DELETE` | `/api/sessions/{id}` | Delete session and all its messages |
| `POST` | `/api/sessions/{id}/archive` | Archive a session |
| `POST` | `/api/sessions/folders` | Create a session folder |
| `PUT` | `/api/sessions/{id}/folder` | Move session to folder |

**Database:** sessions stored in `sessions` table, messages in `messages` table — both in `core/database.py`.

---

## Frontend Connection

The frontend chat UI lives in:
- `static/js/chat.js` — main chat component
- `static/js/chatStream.js` — SSE consumer
- `static/js/chatRenderer.js` — message rendering (Markdown, code blocks, tool output)

See [`../static/README.md`](../static/README.md).

---

## Related

- LLM call internals → [`../src/llm.md`](../src/llm.md)
- Agent loop internals → [`../src/agent.md`](../src/agent.md)
- RAG injection → [`../src/memory-rag.md`](../src/memory-rag.md)
- Session persistence → [`../core/README.md`](../core/README.md)
