# Media Routes (Images, TTS, STT)

> Parent: [`README.md`](README.md) | Architecture: [`../architecture.md`](../architecture.md)

---

## Image Generation

### [`routes/gallery_routes.py`](/routes/gallery_routes.py) (~1785 lines)
Image generation, inpainting, and upscaling via diffusion models.

**Key endpoints:**
| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/gallery/generate` | Text-to-image generation |
| `POST` | `/api/gallery/inpaint` | Inpainting (edit masked region) |
| `POST` | `/api/gallery/upscale` | Upscale an image |
| `GET` | `/api/gallery` | List generated images |
| `GET` | `/api/gallery/{id}` | Get image metadata |
| `DELETE` | `/api/gallery/{id}` | Delete image |

**Backends:** Integrates with AUTOMATIC1111 (Stable Diffusion WebUI), ComfyUI, or any A1111-compatible API. Connection URL configured in Settings → Image Generation.

**Storage:** Generated images saved to `data/generated_images/`.

**Frontend:** `static/js/gallery.js`, `static/js/galleryEditor.js`

---

## Text-to-Speech

### [`routes/tts_routes.py`](/routes/tts_routes.py)
Convert text to audio.

**Supported providers:**
- Local TTS (via system or bundled TTS engine)
- OpenAI TTS API
- ElevenLabs
- Any configured TTS endpoint

**Key endpoints:**
| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/tts/synthesize` | Synthesize text to audio |
| `GET` | `/api/tts/voices` | List available voices |

**Caching:** TTS audio is cached to `data/tts_cache/` to avoid re-synthesizing repeated phrases.

**Service layer:** `services/tts/` — provider-specific wrappers.

---

## Speech-to-Text

### [`routes/stt_routes.py`](/routes/stt_routes.py)
Transcribe audio input.

**Supported providers:**
- OpenAI Whisper API
- Local Whisper (if configured)

**Key endpoints:**
| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/stt/transcribe` | Transcribe audio file |

The frontend microphone recording button uses this endpoint.

---

## Related

- Image tools available to the agent → [`../src/agent.md`](../src/agent.md) → `builtin_actions.py`
- TTS service layer → [`../services/README.md`](../services/README.md)
- Cookbook (model download/serving for local inference) → [`README.md`](README.md) → `cookbook_routes.py`
