import asyncio
import json
import logging
import re
import uuid
import wave
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.database import SessionLocal
from app.models import ProductionStage, StageStatus

logger = logging.getLogger(__name__)
_TASKS: dict[int, asyncio.Task] = {}
_CONTROLS: dict[int, str] = {}

IMAGE_NODE = "269"
AUDIO_NODE = "276"
PROMPT_NODE = "459"
DURATION_NODE = "378"
FPS_NODE = "379"
VIDEO_OUTPUT_NODE = "400"


def _safe(value: str) -> str:
    return re.sub(r"[^\w.-]+", "_", str(value), flags=re.UNICODE).strip("._")[:90] or "scene"


def manifest_path(project_path: Path) -> Path:
    return Path(project_path) / "video" / "video_manifest.json"


def read_manifest(project_path: Path) -> dict[str, Any] | None:
    path = manifest_path(project_path)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not read video manifest: %s", path)
        return None


def write_manifest(project_path: Path, manifest: dict[str, Any]) -> None:
    path = manifest_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _set_stage(project_id: int, status: StageStatus, detail: str) -> None:
    db = SessionLocal()
    try:
        stage = db.query(ProductionStage).filter_by(project_id=project_id, stage_key="video").first()
        if stage:
            stage.status = status
            stage.detail = detail
            db.commit()
    finally:
        db.close()


def video_worker_active(project_id: int) -> bool:
    task = _TASKS.get(project_id)
    return bool(task and not task.done())


class KaggleComfyClient:
    """Client for the Kaggle server's asynchronous /jobs API."""

    def __init__(self):
        self.base = settings.comfyui_base_url.rstrip("/")
        self.api_key = settings.comfyui_api_key.strip()
        if not self.base:
            raise RuntimeError("COMFYUI_BASE_URL is not configured.")
        if not self.api_key:
            raise RuntimeError("COMFYUI_API_KEY is not configured in the local .env file.")
        self.headers = {"X-API-Key": self.api_key}

    async def submit_job(
        self,
        client: httpx.AsyncClient,
        image_path: Path,
        audio_path: Path,
        prompt: str,
    ) -> str:
        # The Kaggle wrapper accepts the selected server-side workflow and media
        # directly. Omitting "workflow" intentionally uses the server default,
        # which is the workflow configured in the user's Kaggle environment.
        data = {"prompt": prompt}
        with image_path.open("rb") as image_stream, audio_path.open("rb") as audio_stream:
            response = await client.post(
                f"{self.base}/jobs",
                headers=self.headers,
                data=data,
                files={
                    "audio": (audio_path.name, audio_stream, "audio/wav"),
                    "image": (image_path.name, image_stream, "image/png"),
                },
                timeout=300.0,
            )
        response.raise_for_status()
        payload = response.json()
        job_id = payload.get("job_id")
        if not job_id:
            raise RuntimeError(f"Kaggle video API did not return job_id: {payload}")
        return str(job_id)

    async def wait_for_video(self, client: httpx.AsyncClient, job_id: str) -> dict:
        timeout_seconds = max(900, int(settings.comfyui_request_timeout_seconds))
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        failures = 0
        while asyncio.get_running_loop().time() < deadline:
            try:
                response = await client.get(
                    f"{self.base}/jobs/{job_id}",
                    headers=self.headers,
                    timeout=60.0,
                )
                response.raise_for_status()
                status = response.json()
                failures = 0
            except (httpx.HTTPError, ValueError) as exc:
                failures += 1
                if failures > 30:
                    raise RuntimeError(f"Lost connection to Kaggle while checking job {job_id}: {exc}") from exc
                await asyncio.sleep(5)
                continue

            state = str(status.get("status") or "").lower()
            detail = status.get("stage") or state
            progress = status.get("progress")
            logger.info("Kaggle job %s: %s; stage=%s; progress=%s", job_id, state, detail, progress)
            if state == "done":
                return status
            if state == "error":
                raise RuntimeError(f"Kaggle video generation failed: {status.get('error') or status}")
            await asyncio.sleep(5)
        raise TimeoutError(f"Kaggle video job {job_id} did not finish within {timeout_seconds} seconds.")

    async def download_video(self, client: httpx.AsyncClient, job_id: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_suffix(destination.suffix + ".tmp")
        try:
            async with client.stream(
                "GET",
                f"{self.base}/jobs/{job_id}/video",
                headers=self.headers,
                timeout=httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0),
            ) as response:
                response.raise_for_status()
                written = 0
                with temp.open("wb") as output:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        if chunk:
                            output.write(chunk)
                            written += len(chunk)
            if written < 1024:
                raise RuntimeError("Kaggle returned an unexpectedly small video file.")
            temp.replace(destination)
        except Exception:
            temp.unlink(missing_ok=True)
            raise


def _scene_prompt(scene: dict, screenplay: dict) -> str:
    prompt = str(scene.get("video_prompt") or "").strip()
    if not prompt:
        prompt = str(scene.get("image_prompt") or scene.get("summary") or "").strip()
    identity = (
        "Use the supplied scene image as the exact first-frame visual reference. "
        "Preserve character identity, clothing, props, location, framing, lighting, "
        "and visual continuity. Animate only the actions described for this scene. "
        "Keep camera movement restrained unless the scene prompt explicitly requests it. "
        "The supplied audio is the exact soundtrack and must remain synchronized."
    )
    return f"{identity}\\n\\nFILM: {screenplay.get('title', '')}\\nSCENE: {scene.get('scene_id', '')}\\n{prompt}"



def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wav_file:
        rate = wav_file.getframerate()
        if rate <= 0:
            raise ValueError(f"Invalid WAV sample rate: {path}")
        return wav_file.getnframes() / float(rate)

def _valid_video(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 1024


async def _run_worker(project_id: int, project_path: Path, screenplay: dict) -> None:
    project_path = Path(project_path)
    manifest = read_manifest(project_path)
    if not manifest:
        return
    _CONTROLS.pop(project_id, None)
    manifest["status"] = "running"
    manifest["error"] = None
    manifest["detail"] = "Connecting to Kaggle ComfyUI..."
    write_manifest(project_path, manifest)
    _set_stage(project_id, StageStatus.running, manifest["detail"])

    try:
        image_manifest_path = project_path / "image_manifest.json"
        audio_manifest_path = project_path / "audio" / "audio_manifest.json"
        if not image_manifest_path.is_file() or not audio_manifest_path.is_file():
            raise RuntimeError("Scene image and audio manifests must both exist before video generation.")
        image_manifest = json.loads(image_manifest_path.read_text(encoding="utf-8"))
        audio_manifest = json.loads(audio_manifest_path.read_text(encoding="utf-8"))
        image_scenes = image_manifest.get("scenes") or {}
        audio_scenes = {
            str(item.get("scene_id")): item
            for item in (audio_manifest.get("scenes") or [])
        }
        scenes = screenplay.get("scenes") or []
        manifest["total"] = len(scenes)
        manifest["completed"] = sum(
            item.get("status") == "completed" and _valid_video(project_path / item.get("video", ""))
            for item in manifest.get("scenes", [])
        )
        manifest["scenes"] = manifest.get("scenes") or []
        records = {str(item.get("scene_id")): item for item in manifest["scenes"]}
        fps = int(settings.default_fps)
        duration_default = int(settings.default_video_scene_seconds)
        client_config = KaggleComfyClient()
        timeout = httpx.Timeout(
            connect=30.0,
            read=max(900.0, float(settings.comfyui_request_timeout_seconds)),
            write=300.0,
            pool=30.0,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            for index, scene in enumerate(scenes, start=1):
                scene_id = str(scene.get("scene_id") or f"SCENE_{index:03d}")
                record = records.setdefault(scene_id, {
                    "scene_id": scene_id,
                    "status": "pending",
                    "video": f"video/{_safe(scene_id)}.mp4",
                })
                destination = project_path / record["video"]
                if record.get("status") == "completed" and _valid_video(destination):
                    continue
                command = _CONTROLS.pop(project_id, None)
                if command in {"pause", "stop"}:
                    status = "paused" if command == "pause" else "stopped"
                    manifest.update(status=status, detail=f"{status.title()} at a safe scene boundary.", current=None)
                    write_manifest(project_path, manifest)
                    _set_stage(project_id, StageStatus.paused if command == "pause" else StageStatus.stopped, manifest["detail"])
                    return

                image_record = image_scenes.get(scene_id) or {}
                audio_record = audio_scenes.get(scene_id) or {}
                image_rel = image_record.get("path")
                audio_rel = audio_record.get("audio")
                image_path = (project_path / image_rel).resolve() if image_rel else None
                audio_path = (project_path / audio_rel).resolve() if audio_rel else None
                if not image_path or not image_path.is_file():
                    raise FileNotFoundError(f"Scene image is missing for {scene_id}: {image_rel}")
                if not audio_path or not audio_path.is_file():
                    raise FileNotFoundError(f"Combined scene audio is missing for {scene_id}: {audio_rel}")
                try:
                    image_path.relative_to(project_path.resolve())
                    audio_path.relative_to(project_path.resolve())
                except ValueError as exc:
                    raise RuntimeError(f"Invalid media path in manifest for {scene_id}") from exc

                # Match the generated clip length to the scene audio unless the screenplay
                # explicitly supplies a duration. This prevents trimming longer dialogue.
                audio_duration = _wav_duration(audio_path)
                duration = float(scene.get("duration_seconds") or scene.get("duration") or audio_duration or duration_default)
                duration = max(1.0, min(duration, 60.0))
                job_id = await client_config.submit_job(
                    client,
                    image_path,
                    audio_path,
                    _scene_prompt(scene, screenplay),
                )
                record.update(status="running", job_id=None, duration_seconds=duration, fps=fps)
                manifest.update(
                    status="running",
                    current={"index": index, "total": len(scenes), "scene_id": scene_id},
                    completed=sum(x.get("status") == "completed" and _valid_video(project_path / x.get("video", "")) for x in manifest["scenes"]),
                    detail=f"Uploading inputs and queueing {scene_id} on Kaggle ComfyUI.",
                )
                write_manifest(project_path, manifest)
                record["job_id"] = job_id
                manifest["detail"] = f"Generating {scene_id} on Kaggle (job {job_id})."
                write_manifest(project_path, manifest)
                await client_config.wait_for_video(client, job_id)
                await client_config.download_video(client, job_id, destination)
                if not _valid_video(destination):
                    raise RuntimeError(f"Downloaded video for {scene_id} is invalid.")
                record.update(
                    status="completed",
                    job_id=job_id,
                    size_bytes=destination.stat().st_size,
                )
                record.pop("error", None)
                manifest["completed"] = sum(
                    x.get("status") == "completed" and _valid_video(project_path / x.get("video", ""))
                    for x in manifest["scenes"]
                )
                manifest["current"] = None
                manifest["detail"] = f"Completed {scene_id} ({manifest['completed']} / {manifest['total']})."
                write_manifest(project_path, manifest)

            manifest.update(status="completed", completed=len(scenes), current=None)
            manifest["detail"] = f"Generated {len(scenes)} scene video clips."
            write_manifest(project_path, manifest)
            _set_stage(project_id, StageStatus.completed, manifest["detail"])
    except Exception as exc:
        logger.exception("Video generation failed for project %s", project_id)
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        manifest["detail"] = f"Video generation failed: {exc}"
        manifest["current"] = None
        write_manifest(project_path, manifest)
        _set_stage(project_id, StageStatus.failed, manifest["detail"])
    finally:
        _TASKS.pop(project_id, None)
        _CONTROLS.pop(project_id, None)


def start_video_worker(project_id: int, project_path: Path, screenplay: dict) -> bool:
    task = _TASKS.get(project_id)
    if task and not task.done():
        return False
    _TASKS[project_id] = asyncio.create_task(_run_worker(project_id, Path(project_path), screenplay))
    return True


def request_video_control(project_id: int, project_path: Path, command: str) -> str:
    if command not in {"pause", "stop"}:
        raise ValueError("Unsupported video control command")
    manifest = read_manifest(Path(project_path))
    if not manifest:
        raise ValueError("Video generation has not been prepared.")
    task = _TASKS.get(project_id)
    if task and not task.done():
        _CONTROLS[project_id] = command
        manifest["status"] = f"{command}_requested"
        manifest["detail"] = f"{command.title()} requested; the current scene clip will finish first."
        write_manifest(Path(project_path), manifest)
        return manifest["status"]
    if manifest.get("status") in {"paused", "stopped"}:
        return manifest["status"]
    raise ValueError(f"Video generation is not running (status: {manifest.get('status')}).")
