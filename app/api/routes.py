
import json
import logging
import shutil
import wave
import httpx
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import (
    Project,
    ProjectStatus,
    ProductionStage,
    StageStatus,
)
from app.schemas import ProjectCreate, ProjectOut, ScreenplayResponse, AudioGenerateRequest
from app.services.gemini import GeminiService
from app.services.image_generation import ImageGenerationService
from app.services.project_files import create_project_workspace
from app.services.tts import VoiceboxTTSProvider, KVoiceTTSProvider
from app.services.kaggle_workers import register_worker, list_workers, worker_count, encrypt_api_key, decrypt_api_key
from app.services.video_production import (
    read_manifest as read_video_manifest,
    write_manifest as write_video_manifest,
    start_video_worker,
    video_worker_active,
    request_video_control,
)
from app.services.audio_production import (
    _flatten,
    audio_worker_active,
    read_manifest,
    required_speakers,
    request_audio_control,
    start_audio_worker,
    write_manifest,
)

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

def _project_provider_config(project: Project) -> dict:
    try:
        saved = json.loads(project.provider_config_json or "{}")
    except (TypeError, json.JSONDecodeError):
        saved = {}
    result = {}
    for name in ("kimage", "kvoice"):
        item = saved.get(name) or {}
        encrypted = item.get("encrypted_api_key") or ""
        key = decrypt_api_key(encrypted) if encrypted else ""
        result[name] = {
            "api_url": item.get("api_url", ""),
            "workflow_json": item.get("workflow_json", ""),
            "api_key": key,
        }
    return result


@router.get("/projects/{project_id}/provider-settings")
def get_provider_settings(project_id: int, db: Session = Depends(get_db)):
    project = get_project_or_404(project_id, db)
    try:
        saved = json.loads(project.provider_config_json or "{}")
    except json.JSONDecodeError:
        saved = {}
    result = {}
    for provider in ("kimage", "kvoice"):
        item = saved.get(provider) or {}
        result[provider] = {
            "api_url": item.get("api_url", ""),
            "workflow_json": item.get("workflow_json", ""),
            "api_key_configured": bool(item.get("encrypted_api_key")),
        }
    result["image_provider"] = saved.get("image_provider", "gemini")
    result["audio_provider"] = saved.get("audio_provider", "voicebox")
    return result


@router.put("/projects/{project_id}/provider-settings")
async def save_provider_settings(project_id: int, request: Request, db: Session = Depends(get_db)):
    project = get_project_or_404(project_id, db)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Settings must be a JSON object.")
    try:
        saved = json.loads(project.provider_config_json or "{}")
    except json.JSONDecodeError:
        saved = {}
    for provider in ("kimage", "kvoice"):
        incoming = payload.get(provider)
        if incoming is None:
            continue
        if not isinstance(incoming, dict):
            raise HTTPException(status_code=400, detail=f"{provider} settings must be an object.")
        current = saved.get(provider) or {}
        url = str(incoming.get("api_url", current.get("api_url", ""))).strip().rstrip("/")
        workflow = incoming.get("workflow_json", current.get("workflow_json", ""))
        api_key = str(incoming.get("api_key") or "").strip()
        if url:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise HTTPException(status_code=400, detail=f"Enter a valid {provider} API URL without embedded credentials.")
            if parsed.scheme == "http" and parsed.hostname.lower() not in {"localhost", "127.0.0.1", "::1"}:
                raise HTTPException(status_code=400, detail="Use HTTPS for remote API servers.")
        if workflow:
            try:
                decoded = json.loads(workflow) if isinstance(workflow, str) else workflow
                if not isinstance(decoded, dict):
                    raise ValueError()
                workflow = json.dumps(decoded, ensure_ascii=False, indent=2)
            except (json.JSONDecodeError, TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{provider} workflow must be valid JSON object.")
        elif workflow == "":
            workflow = ""
        if api_key:
            try:
                encrypted = encrypt_api_key(api_key)
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        else:
            encrypted = current.get("encrypted_api_key", "")
        saved[provider] = {
            "api_url": url,
            "workflow_json": workflow,
            "encrypted_api_key": encrypted,
        }
    image_provider = str(payload.get("image_provider", saved.get("image_provider", "gemini"))).lower()
    audio_provider = str(payload.get("audio_provider", saved.get("audio_provider", "voicebox"))).lower()
    if image_provider not in {"gemini", "kimage"}:
        raise HTTPException(status_code=400, detail="Image provider must be Gemini or kImage.")
    if audio_provider not in {"voicebox", "kvoice"}:
        raise HTTPException(status_code=400, detail="Audio provider must be Voicebox or kVoice.")
    saved["image_provider"] = image_provider
    saved["audio_provider"] = audio_provider
    project.provider_config_json = json.dumps(saved, ensure_ascii=False)
    db.commit()
    return {"ok": True, "message": "Provider settings saved.", **{
        key: {"api_url": saved.get(key, {}).get("api_url", ""),
              "workflow_json": saved.get(key, {}).get("workflow_json", ""),
              "api_key_configured": bool(saved.get(key, {}).get("encrypted_api_key"))}
        for key in ("kimage", "kvoice")
    }}


@router.get("/kaggle/workers")
def get_kaggle_workers():
    return {"workers": list_workers(), "limit": 5}


@router.post("/kaggle/workers/register")
async def register_kaggle_worker(request: Request):
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("Worker registration payload must be a JSON object.")
        worker = await register_worker(
            name=str(payload.get("name") or ""),
            api_url=str(payload.get("api_url") or ""),
            api_key=str(payload.get("api_key") or ""),
        )
        return {"ok": True, "worker": worker}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Kaggle registration request failed: {exc}") from exc
    except Exception as exc:
        logger.exception("Kaggle worker registration failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/kaggle/workers/{worker_id}")
def delete_kaggle_worker(worker_id: int, db: Session = Depends(get_db)):
    from app.models import KaggleWorker
    worker = db.get(KaggleWorker, worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Kaggle worker not found.")
    if worker.status == "BUSY":
        raise HTTPException(status_code=409, detail="Cannot remove a worker while it is processing a scene.")
    db.delete(worker)
    db.commit()
    return {"ok": True}


STAGES = [
    "screenplay",
    "character_bible",
    "scene_images",
    "audio",
    "video",
    "postproduction",
    "final_assembly",
]


def get_project_or_404(project_id: int, db: Session) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def get_stage(project: Project, stage_key: str):
    return next(
        (stage for stage in project.stages if stage.stage_key == stage_key),
        None,
    )


def _video_scene_records(manifest: dict, scenes: list[dict]) -> list[dict]:
    """Keep existing scene records and add records for any new screenplay scenes."""
    existing = {
        str(item.get("scene_id")): item
        for item in (manifest.get("scenes") or [])
        if isinstance(item, dict) and item.get("scene_id") is not None
    }

    ordered = []
    for index, scene in enumerate(scenes, start=1):
        scene_id = str(scene.get("scene_id") or f"SCENE_{index:03d}")
        record = existing.get(scene_id)

        if record is None:
            safe_id = "".join(
                char if char.isalnum() or char in "-_." else "_"
                for char in scene_id
            ).strip("._")[:90] or f"SCENE_{index:03d}"

            record = {
                "scene_id": scene_id,
                "status": "pending",
                "video": f"video/{safe_id}.mp4",
            }

        record.setdefault("scene_id", scene_id)
        record.setdefault("status", "pending")
        record.setdefault("video", f"video/SCENE_{index:03d}.mp4")
        ordered.append(record)

    return ordered


@router.get("/studio")
def studio(request: Request, db: Session = Depends(get_db)):
    projects = db.query(Project).order_by(Project.created_at.desc()).all()
    return templates.TemplateResponse(
        request=request,
        name="studio.html",
        context={"projects": projects},
    )


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)):
    return db.query(Project).order_by(Project.created_at.desc()).all()


@router.post("/projects", response_model=ProjectOut)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    safe_name = "".join(
        c if c.isalnum() or c in "-_" else "_"
        for c in payload.title
    ).strip("_")[:60] or "Story"

    workspace = create_project_workspace(settings.projects_dir, safe_name)

    project = Project(
        title=payload.title,
        story_idea=payload.story_idea,
        target_duration_minutes=payload.target_duration_minutes,
        language=payload.language,
        visual_style=payload.visual_style,
        aspect_ratio=payload.aspect_ratio,
        project_path=str(workspace),
    )

    db.add(project)
    db.flush()

    for key in STAGES:
        db.add(
            ProductionStage(
                project_id=project.id,
                stage_key=key,
                status=StageStatus.pending,
            )
        )

    db.commit()
    db.refresh(project)
    return project


@router.delete("/projects/{project_id}")
def delete_project(project_id: int, db: Session = Depends(get_db)):
    """Permanently delete a project, its database records, and its complete workspace."""
    project = get_project_or_404(project_id, db)

    if audio_worker_active(project_id) or video_worker_active(project_id):
        raise HTTPException(
            status_code=409,
            detail="Cannot delete this project while audio or video production is running. Stop the active production first.",
        )

    if any(stage.status == StageStatus.running for stage in project.stages):
        raise HTTPException(
            status_code=409,
            detail="Cannot delete this project while a production stage is running.",
        )

    project_root = Path(settings.projects_dir).resolve()
    workspace = Path(project.project_path).resolve()
    try:
        workspace.relative_to(project_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Project workspace is outside the configured projects directory.") from exc

    if workspace == project_root:
        raise HTTPException(status_code=400, detail="Refusing to delete the projects directory itself.")

    if workspace.exists():
        shutil.rmtree(workspace)

    db.delete(project)
    db.commit()
    return {"ok": True, "message": "Project and all associated files were deleted."}


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db)):
    return get_project_or_404(project_id, db)


@router.get("/projects/{project_id}/stages")
def get_stages(project_id: int, db: Session = Depends(get_db)):
    project = get_project_or_404(project_id, db)

    return [
        {
            "stage_key": stage.stage_key,
            "status": stage.status.value,
            "detail": stage.detail,
        }
        for stage in project.stages
    ]


@router.post(
    "/projects/{project_id}/screenplay/generate",
    response_model=ScreenplayResponse,
)
async def generate_screenplay(
    project_id: int,
    db: Session = Depends(get_db),
):
    project = get_project_or_404(project_id, db)

    if not settings.gemini_api_key:
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY is not configured in .env",
        )

    stage = get_stage(project, "screenplay")

    if stage:
        stage.status = StageStatus.running
        stage.detail = "Generating screenplay..."
        db.commit()

    try:
        screenplay = await GeminiService().generate_screenplay(
            story_idea=project.story_idea,
            duration_minutes=project.target_duration_minutes,
            language=project.language,
            visual_style=project.visual_style,
            aspect_ratio=project.aspect_ratio,
        )

        project.screenplay_json = json.dumps(
            screenplay,
            ensure_ascii=False,
            indent=2,
        )

        project.status = ProjectStatus.planning

        output = Path(project.project_path) / "screenplay.json"
        output.write_text(
            project.screenplay_json,
            encoding="utf-8",
        )

        if stage:
            stage.status = StageStatus.waiting_approval
            stage.detail = (
                "Screenplay generated. Review and approve before production."
            )

        db.commit()

        return ScreenplayResponse(
            project_id=project.id,
            screenplay=screenplay,
            saved_to=str(output),
        )

    except Exception as exc:
        logger.exception("Screenplay generation failed")

        if stage:
            stage.status = StageStatus.failed
            stage.detail = str(exc)

        db.commit()

        raise HTTPException(
            status_code=502,
            detail=f"Screenplay generation failed: {exc}",
        ) from exc


@router.get("/projects/{project_id}/screenplay")
def get_screenplay(project_id: int, db: Session = Depends(get_db)):
    project = get_project_or_404(project_id, db)

    # Recover screenplay data from disk if the DB column is empty.
    screenplay_data = None
    if project.screenplay_json:
        try:
            screenplay_data = json.loads(project.screenplay_json)
        except json.JSONDecodeError:
            logger.exception("Invalid screenplay JSON in database for project %s", project.id)

    screenplay_file = Path(project.project_path) / "screenplay.json"
    if screenplay_data is None and screenplay_file.is_file():
        try:
            screenplay_data = json.loads(screenplay_file.read_text(encoding="utf-8"))
            project.screenplay_json = json.dumps(screenplay_data, ensure_ascii=False, indent=2)
            db.commit()
        except (OSError, json.JSONDecodeError):
            logger.exception("Could not recover screenplay file for project %s", project.id)

    return {
        "project_id": project.id,
        "generated": screenplay_data is not None,
        "screenplay": screenplay_data,
    }

@router.post("/projects/{project_id}/screenplay/approve")
def approve_screenplay(
    project_id: int,
    db: Session = Depends(get_db),
):
    project = get_project_or_404(project_id, db)

    if not project.screenplay_json:
        raise HTTPException(
            status_code=400,
            detail="Generate the screenplay before approving it.",
        )

    stage = get_stage(project, "screenplay")

    if not stage:
        raise HTTPException(
            status_code=500,
            detail="Screenplay production stage is missing.",
        )

    stage.status = StageStatus.completed
    stage.detail = "Approved by user"
    project.status = ProjectStatus.producing

    db.commit()

    return {
        "ok": True,
        "message": "Screenplay approved. You can now generate visual references.",
    }


@router.post("/projects/{project_id}/references/generate")
async def generate_references(
    project_id: int,
    db: Session = Depends(get_db),
):
    project = get_project_or_404(project_id, db)

    screenplay_stage = get_stage(project, "screenplay")
    reference_stage = get_stage(project, "character_bible")

    if not project.screenplay_json:
        raise HTTPException(
            status_code=400,
            detail="Generate the screenplay first.",
        )

    if not screenplay_stage or screenplay_stage.status != StageStatus.completed:
        raise HTTPException(
            status_code=400,
            detail="Approve the screenplay before generating references.",
        )

    if not reference_stage:
        raise HTTPException(
            status_code=500,
            detail="Character Bible production stage is missing.",
        )

    if reference_stage.status == StageStatus.running:
        raise HTTPException(
            status_code=409,
            detail="Reference generation is already running.",
        )

    screenplay = json.loads(project.screenplay_json)
    project_path = Path(project.project_path)

    reference_stage.status = StageStatus.running
    reference_stage.detail = "Generating character, prop and location references..."
    db.commit()

    try:
        provider_config = _project_provider_config(project)
        service = ImageGenerationService(image_provider=json.loads(project.provider_config_json or "{}").get("image_provider", "gemini"),
                                         kimage_config=provider_config["kimage"])

        result = await service.generate_references(
            project_path=project_path,
            screenplay=screenplay,
            visual_style=project.visual_style,
            aspect_ratio=project.aspect_ratio,
        )

        reference_stage.status = StageStatus.completed
        reference_stage.detail = f"Visual references generated using {project.visual_style}."
        db.commit()

        return {
            "ok": True,
            "message": "Visual references generated successfully.",
            "result": result,
        }

    except Exception as exc:
        logger.exception("Reference generation failed")

        reference_stage.status = StageStatus.failed
        reference_stage.detail = str(exc)
        db.commit()

        raise HTTPException(
            status_code=502,
            detail=f"Reference generation failed: {exc}",
        ) from exc


@router.post("/projects/{project_id}/images/generate")
async def generate_scene_images(
    project_id: int,
    db: Session = Depends(get_db),
):
    project = get_project_or_404(project_id, db)

    screenplay_stage = get_stage(project, "screenplay")
    reference_stage = get_stage(project, "character_bible")
    scene_stage = get_stage(project, "scene_images")

    if not project.screenplay_json:
        raise HTTPException(
            status_code=400,
            detail="Generate the screenplay first.",
        )

    if not screenplay_stage or screenplay_stage.status != StageStatus.completed:
        raise HTTPException(
            status_code=400,
            detail="Approve the screenplay before generating scene images.",
        )

    if not reference_stage or reference_stage.status != StageStatus.completed:
        raise HTTPException(
            status_code=400,
            detail="Generate all character, prop and location references first.",
        )

    if not scene_stage:
        raise HTTPException(
            status_code=500,
            detail="Scene image production stage is missing.",
        )

    if scene_stage.status == StageStatus.running:
        raise HTTPException(
            status_code=409,
            detail="Scene image generation is already running.",
        )

    screenplay = json.loads(project.screenplay_json)
    project_path = Path(project.project_path)

    scene_stage.status = StageStatus.running
    scene_stage.detail = "Generating scene images..."
    db.commit()

    try:
        provider_config = _project_provider_config(project)
        service = ImageGenerationService(image_provider=json.loads(project.provider_config_json or "{}").get("image_provider", "gemini"),
                                         kimage_config=provider_config["kimage"])

        result = await service.generate_scene_images(
            project_path=project_path,
            screenplay=screenplay,
            aspect_ratio=project.aspect_ratio,
            visual_style=project.visual_style,
        )

        scene_stage.status = StageStatus.completed
        scene_stage.detail = "Scene images generated."
        db.commit()

        return {
            "ok": True,
            "message": "Scene images generated successfully.",
            "result": result,
        }

    except Exception as exc:
        logger.exception("Scene image generation failed")

        scene_stage.status = StageStatus.failed
        scene_stage.detail = str(exc)
        db.commit()

        raise HTTPException(
            status_code=502,
            detail=f"Scene image generation failed: {exc}",
        ) from exc


@router.get("/projects/{project_id}/images/status")
def get_image_status(
    project_id: int,
    db: Session = Depends(get_db),
):
    project = get_project_or_404(project_id, db)

    service = ImageGenerationService()
    status = service.get_image_status(Path(project.project_path))

    return {
        "project_id": project.id,
        "project_title": project.title,
        "status": status,
    }


@router.get("/projects/{project_id}/assets/{asset_path:path}")
def get_project_asset(
    project_id: int,
    asset_path: str,
    db: Session = Depends(get_db),
):
    project = get_project_or_404(project_id, db)

    project_root = Path(project.project_path).resolve()
    requested_file = (project_root / asset_path).resolve()

    try:
        requested_file.relative_to(project_root)
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid asset path")

    if not requested_file.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")

    return FileResponse(requested_file)


@router.get("/projects/{project_id}/audio/voices")
async def list_project_audio_voices(project_id: int, provider: str = "voicebox", db: Session = Depends(get_db)):
    project = get_project_or_404(project_id, db)
    if provider == "kvoice":
        config = _project_provider_config(project)["kvoice"]
        try:
            return await KVoiceTTSProvider(config["api_url"], config["api_key"]).list_voices()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Could not load kVoice voices: {exc}") from exc
    try:
        return await VoiceboxTTSProvider().list_profiles()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not load Voicebox profiles: {exc}") from exc


@router.get("/voicebox/profiles")
async def list_voicebox_profiles():
    """Return available local Voicebox voice profiles for the audio-stage selector."""
    try:
        return await VoiceboxTTSProvider().list_profiles()
    except Exception as exc:
        logger.exception("Could not load Voicebox profiles")
        raise HTTPException(
            status_code=503,
            detail=f"Could not connect to Voicebox at {settings.voicebox_base_url}: {exc}",
        ) from exc


@router.post("/projects/{project_id}/audio/generate")
async def generate_project_audio(
    project_id: int,
    payload: AudioGenerateRequest,
    db: Session = Depends(get_db),
):
    """Prepare per-speaker voice assignments and start/resume checkpointed audio."""
    project = get_project_or_404(project_id, db)
    if not project.screenplay_json:
        raise HTTPException(status_code=400, detail="Generate the screenplay before generating audio.")

    stage = get_stage(project, "audio")
    if not stage:
        raise HTTPException(status_code=500, detail="Audio production stage is missing.")
    if audio_worker_active(project_id):
        raise HTTPException(status_code=409, detail="Audio generation is already running.")

    screenplay = json.loads(project.screenplay_json)
    scenes = screenplay.get("scenes") or []
    if not scenes:
        raise HTTPException(status_code=400, detail="The screenplay contains no scenes.")

    provider_settings = _project_provider_config(project)
    audio_provider = str(getattr(payload, "provider", "voicebox") or "voicebox").lower()
    if audio_provider == "kvoice":
        try:
            kvoice = KVoiceTTSProvider(provider_settings["kvoice"]["api_url"], provider_settings["kvoice"]["api_key"])
            profiles = await kvoice.list_voices()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Cannot connect to configured kVoice API: {exc}") from exc
    else:
        audio_provider = "voicebox"
        try:
            profiles = await VoiceboxTTSProvider().list_profiles()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Cannot connect to Voicebox at {settings.voicebox_base_url}: {exc}") from exc

    speakers = required_speakers(screenplay)
    required_ids = [item["id"] for item in speakers]
    missing = [speaker_id for speaker_id in required_ids if not payload.voice_assignments.get(speaker_id)]
    if missing:
        raise HTTPException(
            status_code=400,
            detail="Select a voice profile for every character and the narrator: " + ", ".join(missing),
        )

    profile_map = {str(item.get("id") or item.get("voice_id") or item.get("name")): item for item in profiles}
    invalid_profiles = sorted({
        profile_id for profile_id in payload.voice_assignments.values()
        if profile_id not in profile_map
    })
    if invalid_profiles:
        raise HTTPException(status_code=400, detail="One or more selected Voicebox profiles are unavailable.")

    project_path = Path(project.project_path)
    existing = read_manifest(project_path)
    new_segments = _flatten(screenplay)
    if not new_segments:
        raise HTTPException(status_code=400, detail="The screenplay has no non-empty dialogue or narration segments.")
    same_queue = bool(
        existing
        and str(existing.get("provider") or "voicebox").lower() == audio_provider
        and [
            (x.get("scene_id"), x.get("segment_id"), x.get("speaker"), x.get("text"))
            for x in existing.get("segments", [])
        ] == [
            (x.get("scene_id"), x.get("segment_id"), x.get("speaker"), x.get("text"))
            for x in new_segments
        ]
    )

    if same_queue:
        manifest = existing
        old_assignment_map = {
            item.get("speaker_id"): item.get("profile_id")
            for item in (manifest.get("voice_assignments") or [])
        }
        for item in manifest["segments"]:
            old_profile = old_assignment_map.get(item.get("speaker"))
            new_profile = payload.voice_assignments.get(item.get("speaker"))
            if old_profile != new_profile:
                old_audio = project_path / item.get("audio", "")
                old_audio.unlink(missing_ok=True)
                item["status"] = "pending"
                item.pop("profile_id", None)
                item.pop("error", None)
    else:
        manifest = {
            "provider": "voicebox",
            "segments": new_segments,
            "scenes": [],
            "completed": 0,
            "next_segment_index": 0,
        }

    manifest["voice_assignments"] = [
        {
            "speaker_id": speaker["id"],
            "speaker_name": speaker["name"],
            "kind": speaker["kind"],
            "profile_id": payload.voice_assignments[speaker["id"]],
            "profile_name": profile_map[payload.voice_assignments[speaker["id"]]].get("name", payload.voice_assignments[speaker["id"]]),
        }
        for speaker in speakers
    ]
    manifest["engine"] = payload.engine
    manifest["language"] = project.language
    manifest["total"] = len(manifest["segments"])
    manifest["completed"] = sum(x.get("status") == "completed" for x in manifest["segments"])
    manifest["error"] = None
    manifest["status"] = "running"
    manifest["detail"] = "Audio generation queued."
    write_manifest(project_path, manifest)

    manifest["provider"] = audio_provider
    write_manifest(project_path, manifest)
    worker_config = {
        "provider": audio_provider,
        "api_url": provider_settings["kvoice"]["api_url"] if audio_provider == "kvoice" else "",
        "api_key": provider_settings["kvoice"]["api_key"] if audio_provider == "kvoice" else "",
    }
    if not start_audio_worker(project_id, project_path, project.language, payload.engine, worker_config):
        raise HTTPException(status_code=409, detail="Audio generation is already running.")
    return {
        "ok": True,
        "status": "running",
        "message": "Audio generation started. It will continue in the background.",
        "total": manifest["total"],
        "completed": manifest["completed"],
    }


@router.post("/projects/{project_id}/audio/control/{command}")
def control_project_audio(
    project_id: int,
    command: str,
    db: Session = Depends(get_db),
):
    project = get_project_or_404(project_id, db)
    if command not in {"pause", "stop"}:
        raise HTTPException(status_code=400, detail="Command must be 'pause' or 'stop'.")
    try:
        status = request_audio_control(project_id, Path(project.project_path), command)
        return {"ok": True, "status": status, "message": "Control request accepted. The current segment will finish first."}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/projects/{project_id}/audio/resume")
async def resume_project_audio(
    project_id: int,
    db: Session = Depends(get_db),
):
    project = get_project_or_404(project_id, db)
    project_path = Path(project.project_path)
    manifest = read_manifest(project_path)
    if not manifest:
        raise HTTPException(status_code=404, detail="No audio checkpoint exists. Configure voices and generate audio first.")
    if audio_worker_active(project_id):
        raise HTTPException(status_code=409, detail="Audio generation is already running.")
    if manifest.get("status") == "completed":
        return {"ok": True, "status": "completed", "message": "All audio segments are already completed."}

    engine = manifest.get("engine") or settings.voicebox_engine
    provider_name = str(manifest.get("provider") or "voicebox").lower()
    saved_provider = _project_provider_config(project)
    worker_config = {
        "provider": provider_name,
        "api_url": saved_provider["kvoice"]["api_url"] if provider_name == "kvoice" else "",
        "api_key": saved_provider["kvoice"]["api_key"] if provider_name == "kvoice" else "",
    }
    if not start_audio_worker(project_id, project_path, project.language, engine, worker_config):
        raise HTTPException(status_code=409, detail="Audio generation is already running.")
    return {
        "ok": True,
        "status": "running",
        "message": "Resuming from the first unfinished audio segment.",
        "completed": manifest.get("completed", 0),
        "total": manifest.get("total", len(manifest.get("segments") or [])),
    }


@router.get("/projects/{project_id}/audio")
def get_project_audio(project_id: int, db: Session = Depends(get_db)):
    project = get_project_or_404(project_id, db)
    manifest = read_manifest(Path(project.project_path))
    if not manifest:
        return {
            "project_id": project.id,
            "status": "pending",
            "detail": "Audio has not been generated yet.",
            "segments": [],
            "scenes": [],
            "voice_assignments": [],
            "completed": 0,
            "total": 0,
        }

    if manifest.get("status") in {"running", "pause_requested", "stop_requested"} and not audio_worker_active(project_id):
        manifest["status"] = "paused"
        manifest["detail"] = "Server restarted; safely paused at the last completed segment. Resume to continue."
        write_manifest(Path(project.project_path), manifest)
        stage = get_stage(project, "audio")
        if stage:
            stage.status = StageStatus.paused
            stage.detail = manifest["detail"]
            db.commit()
    return manifest


@router.post("/projects/{project_id}/video/generate")
async def generate_project_video(
    project_id: int,
    db: Session = Depends(get_db),
):
    """Queue scene-by-scene video generation on the Kaggle ComfyUI server."""
    project = get_project_or_404(project_id, db)

    if not project.screenplay_json:
        raise HTTPException(status_code=400, detail="Generate and approve the screenplay first.")
    if worker_count() == 0:
        raise HTTPException(status_code=503, detail="Register at least one Kaggle worker in Video Production.")
    if video_worker_active(project_id):
        raise HTTPException(status_code=409, detail="Video generation is already running.")

    image_stage = get_stage(project, "scene_images")
    audio_stage = get_stage(project, "audio")
    video_stage = get_stage(project, "video")

    if not image_stage or image_stage.status != StageStatus.completed:
        raise HTTPException(status_code=400, detail="Generate all scene images first.")
    if not audio_stage or audio_stage.status != StageStatus.completed:
        raise HTTPException(status_code=400, detail="Complete scene audio generation first.")
    if not video_stage:
        raise HTTPException(status_code=500, detail="Video production stage is missing.")

    project_path = Path(project.project_path)
    screenplay = json.loads(project.screenplay_json)
    scenes = screenplay.get("scenes") or []

    if not scenes:
        raise HTTPException(status_code=400, detail="The screenplay contains no scenes.")

    existing = read_video_manifest(project_path) or {}
    scene_records = _video_scene_records(existing, scenes)

    manifest = {
        **existing,
        "version": existing.get("version", 1),
        "provider": "kaggle_comfyui",
        "status": "running",
        "detail": "Video generation queued.",
        "total": len(scenes),
        "completed": existing.get("completed", 0),
        "current": None,
        "error": None,
        "scenes": scene_records,
    }

    write_video_manifest(project_path, manifest)
    video_stage.status = StageStatus.running
    video_stage.detail = "Scene video generation queued on Kaggle ComfyUI."
    db.commit()

    if not start_video_worker(project_id, project_path, screenplay):
        raise HTTPException(status_code=409, detail="Video generation is already running.")

    return {
        "ok": True,
        "status": "running",
        "message": "Scene video generation started on Kaggle.",
        "total": len(scenes),
        "completed": manifest["completed"],
    }


@router.get("/projects/{project_id}/video")
def get_project_video(project_id: int, db: Session = Depends(get_db)):
    project = get_project_or_404(project_id, db)
    project_path = Path(project.project_path)
    manifest = read_video_manifest(project_path)

    if not manifest:
        return {
            "project_id": project.id,
            "status": "pending",
            "detail": "Scene video clips have not been generated yet.",
            "scenes": [],
            "completed": 0,
            "total": 0,
        }

    if manifest.get("status") in {"running", "pause_requested", "stop_requested"} and not video_worker_active(project_id):
        manifest["status"] = "paused"
        manifest["detail"] = "Studio restarted; safely paused at the last completed scene. Resume to continue."
        write_video_manifest(project_path, manifest)
        stage = get_stage(project, "video")
        if stage:
            stage.status = StageStatus.paused
            stage.detail = manifest["detail"]
            db.commit()

    return manifest


@router.post("/projects/{project_id}/video/scenes/{scene_id}/regenerate")
async def regenerate_project_video_scene(
    project_id: int,
    scene_id: str,
    db: Session = Depends(get_db),
):
    """Regenerate exactly one scene video, leaving all other clips untouched."""
    project = get_project_or_404(project_id, db)

    if not project.screenplay_json:
        raise HTTPException(status_code=400, detail="The screenplay is missing.")
    if worker_count() == 0:
        raise HTTPException(status_code=503, detail="Register at least one Kaggle worker in Video Production.")
    if video_worker_active(project_id):
        raise HTTPException(status_code=409, detail="Video generation is already running.")

    image_stage = get_stage(project, "scene_images")
    audio_stage = get_stage(project, "audio")
    video_stage = get_stage(project, "video")

    if not image_stage or image_stage.status != StageStatus.completed:
        raise HTTPException(status_code=400, detail="Complete scene image generation first.")
    if not audio_stage or audio_stage.status != StageStatus.completed:
        raise HTTPException(status_code=400, detail="Complete scene audio generation first.")
    if not video_stage:
        raise HTTPException(status_code=500, detail="Video production stage is missing.")

    screenplay = json.loads(project.screenplay_json)
    scenes = screenplay.get("scenes") or []
    if not scenes:
        raise HTTPException(status_code=400, detail="The screenplay contains no scenes.")

    scene = next(
        (
            item for item in scenes
            if str(item.get("scene_id")) == str(scene_id)
        ),
        None,
    )
    if scene is None:
        raise HTTPException(status_code=404, detail=f"Scene not found: {scene_id}")

    project_path = Path(project.project_path)
    image_manifest_path = project_path / "image_manifest.json"
    audio_manifest_path = project_path / "audio" / "audio_manifest.json"

    if not image_manifest_path.is_file():
        raise HTTPException(status_code=400, detail="Scene image manifest is missing.")
    if not audio_manifest_path.is_file():
        raise HTTPException(status_code=400, detail="Scene audio manifest is missing.")

    try:
        image_manifest = json.loads(image_manifest_path.read_text(encoding="utf-8"))
        audio_manifest = json.loads(audio_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not read image/audio manifests: {exc}") from exc

    image_scenes = image_manifest.get("scenes") or {}
    audio_scenes = {
        str(item.get("scene_id")): item
        for item in (audio_manifest.get("scenes") or [])
    }

    image_record = image_scenes.get(str(scene_id)) or {}
    audio_record = audio_scenes.get(str(scene_id)) or {}

    image_rel = image_record.get("path")
    audio_rel = audio_record.get("audio")

    if not image_rel:
        raise HTTPException(status_code=400, detail=f"Scene image path is missing for {scene_id}.")
    if not audio_rel:
        raise HTTPException(status_code=400, detail=f"Scene audio path is missing for {scene_id}.")

    project_root = project_path.resolve()
    for relative, label in ((image_rel, "image"), (audio_rel, "audio")):
        resolved = (project_root / relative).resolve()
        try:
            resolved.relative_to(project_root)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid {label} path for {scene_id}.")
        if not resolved.is_file():
            raise HTTPException(status_code=400, detail=f"Scene {label} file is missing for {scene_id}.")

    try:
        with wave.open(str((project_root / audio_rel).resolve()), "rb") as wav_file:
            if wav_file.getframerate() <= 0 or wav_file.getnframes() <= 0:
                raise ValueError("Audio WAV has no usable frames.")
    except (wave.Error, OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Scene audio is not a valid WAV file: {exc}") from exc

    manifest = read_video_manifest(project_path)
    if not manifest:
        manifest = {
            "version": 1,
            "provider": "kaggle_comfyui",
            "status": "pending",
            "detail": "Video generation prepared.",
            "total": len(scenes),
            "completed": 0,
            "current": None,
            "error": None,
            "scenes": [],
        }

    manifest["scenes"] = _video_scene_records(manifest, scenes)
    manifest["total"] = len(scenes)
    manifest["current"] = {
        "scene_id": str(scene_id),
        "index": next(
            index for index, item in enumerate(scenes, start=1)
            if str(item.get("scene_id")) == str(scene_id)
        ),
        "total": len(scenes),
    }
    manifest["status"] = "running"
    manifest["error"] = None
    manifest["detail"] = f"Regeneration queued for {scene_id}."

    write_video_manifest(project_path, manifest)

    if not start_video_worker(
        project_id,
        project_path,
        screenplay,
        only_scene_id=str(scene_id),
    ):
        raise HTTPException(status_code=409, detail="Video generation is already running.")

    video_stage.status = StageStatus.running
    video_stage.detail = f"Regenerating video for {scene_id}."
    db.commit()

    return {
        "ok": True,
        "status": "running",
        "scene_id": str(scene_id),
        "message": f"Regeneration started for {scene_id}. Other scene clips will be preserved.",
    }


@router.post("/projects/{project_id}/video/control/{command}")
def control_project_video(
    project_id: int,
    command: str,
    db: Session = Depends(get_db),
):
    project = get_project_or_404(project_id, db)
    if command not in {"pause", "stop"}:
        raise HTTPException(status_code=400, detail="Command must be 'pause' or 'stop'.")
    try:
        status = request_video_control(project_id, Path(project.project_path), command)
        return {
            "ok": True,
            "status": status,
            "message": "Control request accepted. The current scene clip will finish first.",
        }
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/projects/{project_id}/video/resume")
async def resume_project_video(
    project_id: int,
    db: Session = Depends(get_db),
):
    project = get_project_or_404(project_id, db)

    if worker_count() == 0:
        raise HTTPException(status_code=503, detail="Register at least one Kaggle worker in Video Production.")

    manifest = read_video_manifest(Path(project.project_path))
    if not manifest:
        raise HTTPException(status_code=404, detail="No video checkpoint exists. Start video generation first.")
    if video_worker_active(project_id):
        raise HTTPException(status_code=409, detail="Video generation is already running.")
    if not project.screenplay_json:
        raise HTTPException(status_code=400, detail="The screenplay is missing.")

    screenplay = json.loads(project.screenplay_json)
    if not start_video_worker(
        project_id,
        Path(project.project_path),
        screenplay,
    ):
        raise HTTPException(status_code=409, detail="Video generation is already running.")

    stage = get_stage(project, "video")
    if stage:
        stage.status = StageStatus.running
        stage.detail = "Resuming scene video generation from the last checkpoint."
        db.commit()

    return {
        "ok": True,
        "status": "running",
        "message": "Resuming from the first unfinished scene.",
        "completed": manifest.get("completed", 0),
        "total": manifest.get("total", 0),
    }
