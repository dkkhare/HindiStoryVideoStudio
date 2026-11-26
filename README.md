# Hindi Story Video Studio

A modular FastAPI application scaffold for producing story-driven videos in a configurable language and visual style:
Gemini screenplay planning, character/prop continuity, Gemini image generation,
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

3. Add `GEMINI_API_KEY` and a unique `SECRET_KEY` to `.env`. Generate a secret with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Keep this value stable: it encrypts the Kaggle API keys stored in SQLite. Configure your local Voicebox/F5-TTS endpoint as needed.
4. Start the server:

   ```powershell
   uvicorn app.main:app --reload
   ```

5. Open http://127.0.0.1:8000. API docs: http://127.0.0.1:8000/docs.

## Current scaffold scope

Implemented: project creation/listing, SQLite persistence, settings, web dashboard,
Gemini structured screenplay generation, project folder creation, health checks,
ComfyUI queue/history client foundation, Voicebox audio generation, per-character
voice assignment, narrator voice assignment, and checkpointed audio-stage controls.

Audio generation is performed one spoken segment at a time. Pause and Stop requests
are honored at the current segment boundary; completed WAV files and the audio manifest
are preserved. Resume continues from the first unfinished segment, including after a
server restart. Changing a speaker voice invalidates only that speaker's completed
segments so they can be regenerated with the new profile.

Video rendering now uses the native direct ComfyUI API (not the legacy Kaggle
`/jobs` gateway). In each project open **API Settings → Direct ComfyUI Video** and
save the current ComfyUI API base URL, API key, and API-exported workflow JSON. The
API key is encrypted in the project database; workflow JSON is stored with the project
provider settings. The Cloudflare URL can be changed whenever the hosted ComfyUI
runtime restarts.

Export `LTX23_IA2V_DUAL_T4_multiimage_HQ.json` from ComfyUI using **Workflow →
Export (API)** with all optional image slots enabled. The renderer uploads scene
images and WAV audio through `/upload/image`, queues the API workflow through
`/prompt`, polls `/history/{prompt_id}`, and downloads the completed video through
`/view`. Pause/Stop interrupt the active ComfyUI prompt; Resume continues from the
first unfinished scene. No Kaggle worker registration or `/jobs` endpoint is required.

The exported graph must contain ComfyUI `LoadImage` and `LoadAudio` nodes and a
video output node that writes an MP4-compatible file to history. Prompt, duration and
FPS inputs are detected from common input names; unusual custom-node graphs may need
a small node-mapping adjustment after the workflow is supplied.

## Important integration notes

- Gemini TTS is not voice cloning. Use F5-TTS for reference-audio voice cloning where
  your F5-TTS server exposes a compatible API.
- Gemini image generation uses reference images only when the selected model/API
  supports image inputs. Keep approved references and prompts in each project.
- LTX-2.5 workflows differ by ComfyUI graph and custom nodes. The adapter expects a
  workflow API JSON file with configurable node IDs; inspect your exported workflow
  and update the mapping in `app/services/comfyui.py`.
- Keep scene assets named by stable IDs (`SCENE_001`, `SEG_001`). Never rely on
  provider-generated filenames.
- Always review provider terms, consent, and rights before cloning or synthesizing a
  real person's voice or likeness.

## First API calls

Create project:
```http
POST /api/projects
Content-Type: application/json

{"title":"Example Story","story_idea":"Describe the premise, characters, setting, and central conflict...","target_duration_minutes":5}
```

Generate screenplay:
```http
POST /api/projects/{project_id}/screenplay/generate
```

The returned screenplay is persisted as `screenplay.json` in the project folder.
