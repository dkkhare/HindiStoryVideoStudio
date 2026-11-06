
import json
import logging
import wave
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
from app.services.project_files import create_project_workspace\nfrom app.services.tts import VoiceboxTTSProvider

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

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

    if not project.screenplay_json:
        raise HTTPException(
            status_code=404,
            detail="Screenplay has not been generated yet.",
        )

    return {
        "project_id": project.id,
        "screenplay": json.loads(project.screenplay_json),
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
        service = ImageGenerationService()

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
        service = ImageGenerationService()

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


def _combine_scene_wavs(input_paths: list[Path], output_path: Path) -> bool:
    """Join compatible PCM WAV segments with a short pause between speakers."""
    if not input_paths:
        return False
    try:
        with wave.open(str(input_paths[0]), "rb") as first:
            params = first.getparams()
            frames = [first.readframes(first.getnframes())]
        for path in input_paths[1:]:
            with wave.open(str(path), "rb") as source:
                if (
                    source.getnchannels() != params.nchannels
                    or source.getsampwidth() != params.sampwidth
                    or source.getframerate() != params.framerate
                    or source.getcomptype() != params.comptype
                ):
                    return False
                frames.append(source.readframes(source.getnframes()))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        silence = bytes(int(params.nchannels * params.sampwidth * params.framerate * 0.25))
        with wave.open(str(output_path), "wb") as target:
            target.setparams(params)
            for index, chunk in enumerate(frames):
                if index:
                    target.writeframes(silence)
                target.writeframes(chunk)
        return True
    except (wave.Error, OSError):
        logger.exception("Could not combine scene WAV files: %s", output_path)
        return False


@router.post("/projects/{project_id}/audio/generate")
async def generate_project_audio(
    project_id: int,
    payload: AudioGenerateRequest,
    db: Session = Depends(get_db),
):
    """Generate all screenplay dialogue/narration segments, one at a time."""
    project = get_project_or_404(project_id, db)
    if not project.screenplay_json:
        raise HTTPException(
            status_code=400,
            detail="Generate the screenplay before generating audio.",
        )

    stage = get_stage(project, "audio")
    if not stage:
        raise HTTPException(status_code=500, detail="Audio production stage is missing.")
    if stage.status == StageStatus.running:
        raise HTTPException(status_code=409, detail="Audio generation is already running.")

    screenplay = json.loads(project.screenplay_json)
    scenes = screenplay.get("scenes") or []
    if not scenes:
        raise HTTPException(status_code=400, detail="The screenplay contains no scenes.")

    voicebox = VoiceboxTTSProvider()
    try:
        profiles = await voicebox.list_profiles()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Cannot connect to Voicebox at {settings.voicebox_base_url}: {exc}",
        ) from exc

    profile = next((item for item in profiles if item.get("id") == payload.profile_id), None)
    if not profile:
        raise HTTPException(status_code=400, detail="The selected Voicebox voice profile was not found.")

    audio_root = Path(project.project_path) / "audio"
    audio_root.mkdir(parents=True, exist_ok=True)
    stage.status = StageStatus.running
    stage.detail = f"Generating audio with Voicebox profile '{profile.get('name', payload.profile_id)}'..."
    db.commit()

    manifest = {
        "provider": "voicebox",
        "profile_id": payload.profile_id,
        "profile_name": profile.get("name", payload.profile_id),
        "engine": payload.engine,
        "language": VoiceboxTTSProvider.language_code(project.language),
        "scenes": [],
    }

    try:
        for scene_index, scene in enumerate(scenes, start=1):
            scene_id = str(scene.get("scene_id") or f"SCENE_{scene_index:03d}")
            safe_scene_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in scene_id)
            scene_dir = audio_root / safe_scene_id
            scene_dir.mkdir(parents=True, exist_ok=True)
            scene_segments = []
            wav_paths = []

            for segment_index, segment in enumerate(scene.get("dialogue_and_narration") or [], start=1):
                text_value = str(segment.get("text") or "").strip()
                if not text_value:
                    continue

                speaker = str(segment.get("speaker") or "NARRATOR")
                safe_speaker = "".join(c if c.isalnum() or c in "-_" else "_" for c in speaker)
                filename = f"segment_{segment_index:03d}_{safe_speaker}.wav"
                output_path = scene_dir / filename

                audio_bytes = await voicebox.synthesize_bytes(
                    text=text_value,
                    profile_id=payload.profile_id,
                    language=project.language,
                    engine=payload.engine,
                )
                output_path.write_bytes(audio_bytes)
                wav_paths.append(output_path)
                scene_segments.append({
                    "segment_id": segment.get("segment_id") or f"SEG_{segment_index:03d}",
                    "speaker": speaker,
                    "kind": segment.get("kind"),
                    "text": text_value,
                    "audio": f"audio/{safe_scene_id}/{filename}".replace("\\\\", "/"),
                })

            combined_name = f"{safe_scene_id}.wav"
            combined_path = audio_root / combined_name
            combined_ok = _combine_scene_wavs(wav_paths, combined_path)
            manifest["scenes"].append({
                "scene_id": scene_id,
                "audio": f"audio/{combined_name}" if combined_ok else None,
                "segments": scene_segments,
            })
            stage.detail = (
                f"Voicebox audio: completed scene {scene_index} of {len(scenes)} "
                f"({len(scene_segments)} spoken segments)."
            )
            db.commit()

        manifest_path = audio_root / "audio_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        stage.status = StageStatus.completed
        stage.detail = (
            f"Audio generated for {len(manifest['scenes'])} scenes using "
            f"Voicebox profile '{profile.get('name', payload.profile_id)}'."
        )
        db.commit()
        return {
            "ok": True,
            "message": stage.detail,
            "manifest": manifest,
        }

    except Exception as exc:
        logger.exception("Project audio generation failed for project %s", project_id)
        manifest["error"] = str(exc)
        (audio_root / "audio_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        stage.status = StageStatus.failed
        stage.detail = f"Audio generation failed: {exc}"
        db.commit()
        raise HTTPException(status_code=502, detail=stage.detail) from exc


@router.get("/projects/{project_id}/audio")
def get_project_audio(project_id: int, db: Session = Depends(get_db)):
    project = get_project_or_404(project_id, db)
    manifest_path = Path(project.project_path) / "audio" / "audio_manifest.json"
    if not manifest_path.is_file():
        raise HTTPException(status_code=404, detail="No audio manifest has been generated yet.")
    return json.loads(manifest_path.read_text(encoding="utf-8"))
