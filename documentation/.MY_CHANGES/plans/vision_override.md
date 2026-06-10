# Plan: Implement Force Vision Override

## Overview
This feature allows users to manually override how images are processed when sent to an LLM. Specifically, it gives users a choice between:
1.  **Force vision capabilities**: Skip the "is_vision_capable" check and always include raw Base64 image data in the prompt (useful for local models that support vision but aren't recognized as such).
2.  **Force non-vision capabilities**: Force the system to run an intermediate Vision-to-Text description pass, even if it detects that the model *could* handle raw images.
3.  **Default (Current)**: Use the existing logic where the presence of "raw" data depends on whether the specific model is identified as vision_capable in `src/integrations.py`.

## Goal
Ensure that users with local models (via Ollama, etc.) can easily bypass the "Intelligence Paradox" by selecting a mode that guarantees their image's raw content reaches the LLM directly.

## Implementation Steps

### 1. Configuration Updates
- Modify `src/settings.py` to add a new setting: `vision_override`.
- The valid options for this setting will be: `"force_vision"`, `"force_no_vision"`, and `"default"`.

### 2. Backend Logic (src/document_processor.py)
Modify the logic that constructs content when an image is attached:
- In `build_user_content`, check the current `vision_override` setting.
- If mode is `force_vision`: Skip the conditional checks for `vision_capable`. If a request contains image data, append it to the prompt as Base64 immediately.
- If mode is `force_no_vision`: Force the execution of `analyze_image_with_vl` and append the resulting text description instead of (or in addition to) raw image data.
- If mode is `default`: Keep current logic where existence of base64 images depends on `vision_capable`.

**Core Principles for File Handling:**
1. **No Pre-processing**: The application must not perform "pre-processing" on files (such as resizing, cropping, or limiting count) to accommodate specific model limitations. If a file is valid per the API's core specifications, it should be passed through intact.
2. **Infrastructure Responsibility**: Any content limitations (e.g., max image resolution, maximum number of items, or total payload size) are the responsibility of the model provider's architecture/API layer. The application will not bake "safety" layers into our code to preemptively modify data for their requirements.
3. **Transparent Error Reporting**: If an error occurs during processing (e.g., a remote API returns a 413 Payload Too Large, or a local file is missing), the system must report that specific error to the UI rather than falling back to generic messages like `[Image not processed]`.

### 3. UI Update (static/app.js & CSS)
- Locate the button group that contains "web" and "bash" toggles in `static/app.js`.
- Add a new selection or toggle for "Vision Mode". Given space constraints, this may be a dropdown or a set of three buttons depending on how much UI space is available.
- Map this to a new state key (e.g., `vision_override`) which will be persisted via `saveToolPref`.
- Update the CSS/Styles to ensure the layout remains consistent when adding this third option.

### 4. Validation & Testing
- Test **Default**: Ensure a standard LLM (like GPT-4o) still gets raw images.
- Test **Force Vision**: Confirm that a local model receives raw base64 even if it's not on the "official" vision list.
- Test **Force No-Vision**: Verify that for both cloud and local models, we get an automated description instead of raw data.
- Verify that `page_08.jpg` still produces a valid result in your preferred mode.

## Component Impact
- `src/settings.py`: Data structure update.
- `src/document_processor.py`: Core logic for processing images and building prompts.
- `static/app.js`: Client-side UI interactions for the toggle buttons.