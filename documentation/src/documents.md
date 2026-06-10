# Document Processing & File Uploads (`src/`)

> Parent: [`README.md`](README.md) | Architecture: [`../architecture.md`](../architecture.md)

---

## Files

### [`src/document_processor.py`](/src/document_processor.py) (~533 lines)
Multi-format document parser. Converts uploaded files into clean Markdown text for indexing and display.

**Supported formats:**
| Format | Parser |
|--------|--------|
| PDF | `pypdf` |
| HTML | `beautifulsoup4` |
| CSV | built-in csv module |
| Markdown | passthrough |
| DOCX, PPTX, XLSX | `markitdown` |
| Plain text | passthrough |

**Key function:**
- `process_document(file_path, mime_type) -> str` — returns Markdown string

After processing, the text is chunked and indexed in ChromaDB via `src/rag_vector.py` for document RAG search.

---

### [`src/upload_handler.py`](/src/upload_handler.py) (~655 lines)
Handles file upload requests from the frontend.

**What it does:**
1. Receives uploaded file (multipart form)
2. Validates file type and size
3. Saves to `data/uploads/`
4. Detects if the file is an image → routes to vision processing
5. For documents → calls `document_processor.py`
6. For images → optionally sends to vision-capable LLM for description/OCR
7. Returns structured metadata (file ID, type, preview text)

**Vision integration:**
If the active model supports vision (detected via `src/integrations.py`), images are encoded as base64 and included in the LLM message directly. Otherwise, an OCR/description pass runs via a separate vision model if configured.

**Storage:** files saved to `data/uploads/{user_id}/{file_id}.{ext}`

---

### [`src/email_thread_parser.py`](/src/email_thread_parser.py) (~614 lines)
Reconstructs email threads from raw IMAP messages.

**What it does:**
- Parses MIME email structures (multipart, attachments, inline images)
- Threads emails by `In-Reply-To` / `References` headers
- Returns a structured thread object with sender, recipients, body (text + HTML), and attachments

Used by `routes/email_routes.py` when loading email conversations.

---

## Personal Document Library

Users can add files to their **personal document library** (`data/personal_docs/`). These are:
- Processed by `document_processor.py`
- Indexed in ChromaDB as the `documents` collection
- Searchable via RAG in chat
- Manageable via `routes/document_routes.py`

The document editor (in-app) also uses this path for `.md`, `.html`, and `.csv` files.

---

## Related

- Vector indexing of documents → [`memory-rag.md`](memory-rag.md)
- Document editor routes → [`../routes/README.md`](../routes/README.md) → `document_routes.py`
- Upload routes → [`../routes/README.md`](../routes/README.md) → `upload_routes.py`
- Email parsing → [`../routes/email.md`](../routes/email.md)
