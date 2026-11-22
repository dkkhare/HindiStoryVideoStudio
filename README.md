# Hindi Story Video Studio

A modular FastAPI application scaffold for producing Hindi story videos:
Gemini screenplay planning, character/prop continuity, ComfyUI scene-image generation,
Gemini TTS or F5-TTS audio, ComfyUI/LTX-2.5 video generation, and FFmpeg post-production.

## Quick start (Windows)

1. Install Python 3.11 or 3.12 and FFmpeg. Ensure `ffmpeg` and `ffprobe` are on PATH.
2. Create an environment and install dependencies:

   ```powershell
   py -3.11 -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   Copy-Item .env.example .env
   ```

3. Add `GEMINI_API_KEY` to `.env`. Configure ComfyUI and F5-TTS endpoints.
4. Start the server:

   ```powershell
   uvicorn app.main:app --reload
   ```

5. Open http://127.0.0.1:8000. API docs: http://127.0.0.1:8000/docs.

## Current scaffold scope

Implemented: project creation/listing, SQLite persistence, settings, web dashboard,
Gemini structured screenplay generation, project folder creation, health checks,
ComfyUI queue/history client foundation, TTS provider interfaces, production-stage
records and resumable stage state.

Scene-image generation is routed through the generic ComfyUI API workflow by default.
The Studio reads each scene's `characters` array and its asset-bible location mapping,
then selects up to four character reference images and one location reference image.
Unused IPAdapter stages are bypassed. A scene with no usable references is generated
from its text prompt alone. Workflow node IDs are discovered from the API graph at
runtime; scene names, character IDs, prompts, and image filenames are not hardcoded.

Configure the following in the Studio `.env` file (use the actual path on your PC):

```env
COMFYUI_BASE_URL=http://127.0.0.1:8188
COMFYUI_IMAGE_WORKFLOW_JSON=D:/studio/ComfyUI_windows_portable/ComfyUI/automatic_reference_api.json
SCENE_IMAGE_PROVIDER=comfyui
COMFYUI_IMAGE_TIMEOUT_SECONDS=900
```

Export the generic reference workflow from ComfyUI using `File → Export (API)`.
It must contain one KSampler, positive and negative CLIPTextEncode nodes, and a model
chain with up to four character IPAdapterAdvanced stages plus one location stage.
Each adapter image input must be connected to a LoadImage node. The Studio uploads
only selected images to ComfyUI and bypasses unused stages, so no placeholder mask or
image files are needed.

The existing `COMFYUI_WORKFLOW_JSON` setting remains reserved for LTX video jobs.
No API keys are stored in the database or source code.

## Important integration notes

- Character, prop, and location master-reference creation remains in the existing
  reference-generation stage; ComfyUI is used for final scene-frame generation.
- The scene prompt retains the complete screenplay visual locks, including props,
  even though the current reference workflow exposes character and location slots.
- Gemini TTS is not voice cloning. Use F5-TTS for reference-audio voice cloning where
  your F5-TTS server exposes a compatible API.
- LTX-2.5 workflows differ by ComfyUI graph and custom nodes. The separate video
  adapter continues to use the configured LTX API workflow.
- Keep scene assets named by stable IDs (`SCENE_001`, `SEG_001`). Never rely on
  provider-generated filenames.
- Always review provider terms, consent, and rights before cloning or synthesizing a
  real person's voice or likeness.

## First API calls

Create project:
```http
POST /api/projects
Content-Type: application/json

{"title":"Thirsty Crow","story_idea":"A thirsty crow finds a pot...","target_duration_minutes":5}
```

Generate screenplay:
```http
POST /api/projects/{project_id}/screenplay/generate
```

The returned screenplay is persisted as `screenplay.json` in the project folder.
