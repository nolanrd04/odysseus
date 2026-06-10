# Vision Override & PDF/Image Pipeline Fixes

**Sessions:** `2026_6_8.1` → `2026_6_8.2` → `2026_6_9.1`
**Status:** Complete
**Handoffs:** `documentation/.SESSION_HANDOFFS/vison_override/`

---

## Problem

Local vision models and Claude Fable 5 were not receiving images or PDFs in the chat payload even when they are capable of processing them. Gemini worked because it is more lenient with the OpenAI-compatible `image_url` format. Three separate bugs caused this, plus the vision override feature built in sessions 8.1–8.2 was non-functional due to a fourth bug.

---

## Session Summaries

### Session 2026_6_8.1 — Backend Infrastructure
Identified the "Intelligence Paradox" (vision models treated as text-only). Built the backend foundation for a user-facing vision override: added `vision_override` to `settings.py` and `_PER_USER_KEYS`, implemented override logic in `chat_handler.py`, and added the eye-icon button to `index.html`.

### Session 2026_6_8.2 — Client-Side Override Button
Implemented the 3-state cycling logic (`default` → `force_vision` → `force_no_vision`) in `app.js` using the `Storage` module. Button persisted state to localStorage and updated the hidden `#vision-override` checkbox for consistency with other tool toggles.

### Session 2026_6_9.1 — Bug Fixes, UI Config, Button Removal
Diagnosed all root causes. Fixed three bugs in the pipeline. Added a UI-configurable vision keyword field in Settings. Removed the override button as redundant.

---

## All Code Changes

### `src/settings.py`
- Added `"vision_override": "default"` to `DEFAULT_SETTINGS`
- Added `"vision_override"` to `_PER_USER_KEYS` so it resolves per-user
- Added `"vision_model_extra_keywords": ""` to `DEFAULT_SETTINGS` — stores user-defined comma-separated model name substrings that get treated as vision-capable at runtime

### `src/chat_handler.py`
- Added `vision_override = get_setting("vision_override", "default")` and override logic (`force_vision` sets `main_is_vision = True`, `force_no_vision` sets it `False`) — **session 8.1**
- Added `[vision-diag]` logger line that includes the override value
- **Session 9.1 bug fix:** Changed `get_setting("vision_enabled")` and `get_setting("vision_override")` to `get_user_setting(key, owner)` so per-user preferences are actually read (previously always read the global default)
- Changed `get_setting("vision_model")` to `get_user_setting("vision_model", owner)` for consistency

### `src/chat_helpers.py`
- Added `"claude-fable"` to `_VISION_MODEL_KEYWORDS` — Anthropic Fable models were not recognized as vision-capable
- Added `"gemma4"`, `"gemma-4"` to `_VISION_MODEL_KEYWORDS` — Gemma 4 was missing (Gemma 3 was already there)
- Updated `is_vision_model()` to check `vision_model_extra_keywords` from settings as a final step after the hardcoded list and the `_VISION_VL_RE` regex — enables UI-driven keyword additions without code changes

### `src/llm_core.py`
- Fixed `_convert_openai_content_to_anthropic()`: when `media_type == "application/pdf"`, emits `"type": "document"` instead of `"type": "image"` — Anthropic's API requires `document` for PDFs; `image` only accepts `image/jpeg`, `image/png`, etc. This was why PDFs reached Gemini but not Claude.

### `static/index.html`
- **Session 8.1:** Added `#vision-override-btn` (eye icon) to the chat toolbar input buttons
- **Session 8.1:** Added `#vision-override` hidden checkbox alongside `#web-toggle` and `#bash-toggle`
- **Session 9.1:** Removed both — button and hidden checkbox deleted
- **Session 9.1:** Added `#set-visionExtraKeywords` text input to the Vision card in the AI defaults settings panel (`data-settings-panel="ai"`)

### `static/app.js`
- **Session 8.2:** Added `visionOverrideBtn` event handler: 3-state cycling, localStorage persistence via `saveToggleState`, hidden checkbox sync
- **Session 9.1:** Added `PUT /api/prefs/vision_override` fetch call on button click to persist preference server-side (bug fix — state was localStorage-only before this)
- **Session 9.1:** Removed entire `visionOverrideBtn` block when button was deleted

### `static/js/settings.js`
- Added `extraKeywordsInput` binding for `#set-visionExtraKeywords`
- Loads `vision_model_extra_keywords` from settings API on panel open
- Saves `vision_model_extra_keywords` with existing vision settings on `change`

---

## Architecture Notes

**Vision capability resolution order** (as of session 9.1):
1. LM Studio endpoint API — reads the `capabilities.vision` flag directly if the endpoint is LM Studio
2. `_VISION_MODEL_KEYWORDS` hardcoded tuple in `src/chat_helpers.py`
3. `_VISION_VL_RE` regex — catches `*-VL-*` / `*VLM*` family names
4. `vision_model_extra_keywords` setting — user-configured patterns from Settings → AI defaults → Vision → Extra patterns

**PDF handling by provider:**
- Gemini / OpenAI-compat endpoints: PDF sent as `image_url` block with `data:application/pdf;base64,...`
- Anthropic: PDF sent as `document` block (fixed in session 9.1 — was incorrectly sent as `image`)

**The "Vision Model" setting** (`Settings → AI defaults → Vision → Model`) is the fallback *describer* — it is only invoked when the chat model is not recognized as vision-capable. It generates a text description of the image that gets injected into the prompt instead. It is NOT a pass-through setting.
