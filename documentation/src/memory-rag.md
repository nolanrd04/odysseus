# Vector Memory & RAG (`src/`)

> Parent: [`README.md`](README.md) | Architecture: [`../architecture.md`](../architecture.md)

---

## Overview

Odysseus maintains a persistent vector memory (ChromaDB) that the LLM can query via RAG (Retrieval-Augmented Generation). Relevant memories are injected into the system prompt on each chat turn. Users can also explicitly add, view, and delete memories.

Two embedding backends are supported:
- **Local** — fastembed (ONNX models, no external API needed)
- **Remote** — any OpenAI-compatible embedding API

---

## Files

### [`src/embeddings.py`](/src/embeddings.py)
Embedding provider abstraction.

**What it does:**
- If `EMBEDDING_URL` is set → calls remote embedding API (OpenAI-compatible)
- Otherwise → uses fastembed with the ONNX model specified by `FASTEMBED_MODEL`
- Returns float vectors for text inputs

**Key function:**
- `get_embeddings(texts: list[str]) -> list[list[float]]`

This is the single embedding interface used by `rag_vector.py` and anywhere else embeddings are needed.

---

### [`src/rag_vector.py`](/src/rag_vector.py) (~501 lines)
ChromaDB-backed vector search with keyword fallback.

**What it does:**
- Stores text chunks as vectors in ChromaDB (`data/chroma/`)
- On query: embeds the query, performs cosine similarity search, returns top-k chunks
- **Keyword fallback:** if vector search returns low-confidence results, falls back to BM25-style keyword search over the same collection
- Manages multiple collections: `memories`, `documents`, `skills`

**Key functions:**
- `add_memory(text, metadata)` — embeds and stores a memory
- `search_memory(query, top_k)` — retrieves relevant memories
- `add_document(doc_id, chunks, metadata)` — indexes a document for RAG
- `search_documents(query, top_k)` — retrieves relevant document chunks

---

### [`services/memory/`](/services/memory/)
ChromaDB client wrapper and collection management. `rag_vector.py` uses this service layer for all ChromaDB interactions.

---

## Configuration

| Env Var | Purpose | Default |
|---------|---------|---------|
| `EMBEDDING_URL` | Remote embedding API base URL | — (uses local) |
| `EMBEDDING_MODEL` | Model name for remote embedding API | — |
| `FASTEMBED_MODEL` | ONNX model for local embeddings | `BAAI/bge-small-en-v1.5` |
| `CHROMADB_HOST` | ChromaDB host (Docker mode) | `localhost` |
| `CHROMADB_PORT` | ChromaDB port | `8000` |

In Docker, ChromaDB runs as a separate container and these are set automatically by `docker-compose.yml`.

---

## Collections

| Collection | Content | Used by |
|-----------|---------|--------|
| `memories` | User-added persistent memories | All chat sessions (RAG injection) |
| `documents` | Indexed personal documents | Document search, RAG |
| `skills` | User-defined skills | Agent tool selection |

---

## RAG Injection Flow

```
ai_interaction.py → inject_rag_context(query)
  → rag_vector.py → search_memory(query)
    → embeddings.py → get_embeddings(query)
    → ChromaDB cosine search
  → top-k results prepended to system prompt
  → LLM sees relevant memories without explicit user retrieval
```

---

## Memory Management Routes

Users can manage memories via the UI. Routes are in `routes/memory_routes.py`:
- `GET /api/memory` — list memories
- `POST /api/memory` — add memory
- `DELETE /api/memory/{id}` — remove memory
- `POST /api/memory/import` — bulk import
- `GET /api/memory/export` — export all memories

See [`../routes/README.md`](../routes/README.md).

---

## Related

- Document indexing for RAG → [`documents.md`](documents.md)
- Memory routes → [`../routes/README.md`](../routes/README.md)
- Services layer → [`../services/README.md`](../services/README.md)
