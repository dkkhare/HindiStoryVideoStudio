import asyncio
import json
import logging
import mimetypes
import re
import wave
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from app.config import settings
from app.database import SessionLocal
from app.models import ProductionStage, StageStatus
from app.services.kaggle_workers import reserve_worker, release_worker, update_worker_job, worker_count

logger = logging.getLogger(__name__)

_TASKS: dict[int, asyncio.Task] = {}
_CONTROLS: dict[int, str] = {}

IMAGE_NODE = "269"
AUDIO_NODE = "276"
PROMPT_NODE = "459"
DURATION_NODE = "378"
FPS_NODE = "379"
VIDEO_OUTPUT_NODE = "400"

import asyncio
import json
import logging
import mimetypes
import re
import wave
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from app.config import settings
from app.database import SessionLocal
from app.models import ProductionStage, StageStatus
from app.services.kaggle_workers import reserve_worker, release_worker, update_worker_job, worker_count

logger = logging.getLogger(__name__)

_TASKS: dict[int, asyncio.Task] = {}
_CONTROLS: dict[int, str] = {}

IMAGE_NODE = "269"
AUDIO_NODE = "276"
PROMPT_NODE = "459"
DURATION_NODE = "378"
FPS_NODE = "379"
VIDEO_OUTPUT_NODE = "400"

DEFAULT_WORKFLOW_ID = "df6f47b947d14adc8b483c1a768673a9"


def _get_active_workflow_id() -> str:
    """Retrieve the latest registered workflow ID from the database dynamically."""
    try:
        db_path = Path("data/studio.db")
        if db_path.is_file():
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM app_settings WHERE key = 'active_workflow_id'")
            row = cursor.fetchone()
            conn.close()
            if row and row[0]:
                return str(row[0]).strip()
    except Exception:
        logger.exception("Failed to retrieve active workflow ID from database, falling back.")

    return str(
        getattr(settings, "comfyui_workflow_id", "")
        or DEFAULT_WORKFLOW_ID
    ).strip()


def _safe(value: str) -> str:
    return (
        re.sub(r"[^\w.-]+", "_", str(value), flags=re.UNICODE)
        .strip("._")[:90]
        or "scene"
    )


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
    temp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(path)


def _set_stage(
    project_id: int,
    status: StageStatus,
    detail: str,
) -> None:
    db = SessionLocal()
    try:
        stage = (
            db.query(ProductionStage)
            .filter_by(project_id=project_id, stage_key="video")
            .first()
        )
        if stage:
            stage.status = status
            stage.detail = detail
            db.commit()
    finally:
        db.close()


def video_worker_active(project_id: int) -> bool:
    task = _TASKS.get(project_id)
    return bool(task and not task.done())


def _valid_video(path: Path) -> bool:
    """Reject missing, empty, and obviously invalid MP4 outputs."""
    try:
        if not path.is_file() or path.stat().st_size < 1024:
            return False

        with path.open("rb") as stream:
            header = stream.read(64)

        return b"ftyp" in header

    except OSError:
        return False


def _safe_media_path(project_path: Path, relative: str) -> Path:
    root = Path(project_path).resolve()
    target = (root / str(relative)).resolve()

    try:
        target.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"Invalid media path: {relative}") from exc

    return target


def _completed_count(
    project_path: Path,
    manifest: dict,
) -> int:
    count = 0

    for item in manifest.get("scenes", []):
        if item.get("status") != "completed":
            continue

        try:
            video_path = _safe_media_path(
                project_path,
                item.get("video", ""),
            )
        except (RuntimeError, TypeError, ValueError):
            continue

        if _valid_video(video_path):
            count += 1

    return count


class KaggleComfyClient:
    """Client for the Kaggle server's asynchronous /jobs API."""

    def __init__(self):
        self.base = settings.comfyui_base_url.rstrip("/")
        self.api_key = settings.comfyui_api_key.strip()

        if not self.base:
            raise RuntimeError("COMFYUI_BASE_URL is not configured.")

        if not self.api_key:
            raise RuntimeError(
                "COMFYUI_API_KEY is not configured in the local .env file."
            )

        self.headers = {"X-API-Key": self.api_key}

    async def submit_job(
        self,
        client: httpx.AsyncClient,
        image_path: Path,
        audio_path: Path,
        prompt: str,
        duration: float,
        fps: int,
    ) -> str:

        # Dynamically fetch the latest workflow ID from the database right before posting
        current_workflow_id = _get_active_workflow_id()
        if not current_workflow_id:
            raise RuntimeError("COMFYUI_WORKFLOW_ID is not configured.")

        inputs = {
            f"{DURATION_NODE}.value": float(duration),
            f"{FPS_NODE}.value": int(fps),
            f"{PROMPT_NODE}.value": str(prompt),
        }

        file_bindings = {
            "image": {
                "node_id": IMAGE_NODE,
                "input_name": "image",
                "upload_type": "image",
            },
            "audio": {
                "node_id": AUDIO_NODE,
                "input_name": "audio",
                "upload_type": "input",
            },
        }

        form_data = {
            "workflow_id": current_workflow_id,
            "inputs": json.dumps(inputs, ensure_ascii=False),
            "file_bindings": json.dumps(file_bindings),
        }

        image_mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
        audio_mime = mimetypes.guess_type(audio_path.name)[0] or "audio/wav"

        logger.info(
            "Submitting Kaggle job: workflow=%s duration=%.2f fps=%s",
            current_workflow_id,
            duration,
            fps,
        )

        with image_path.open("rb") as image_stream, audio_path.open("rb") as audio_stream:
            response = await client.post(
                f"{self.base}/jobs",
                headers=self.headers,
                data=form_data,
                files={
                    "image": (image_path.name, image_stream, image_mime),
                    "audio": (audio_path.name, audio_stream, audio_mime),
                },
                timeout=300.0,
            )

        if response.is_error:
            logger.error(
                "Kaggle /jobs rejected request: HTTP %s | %s",
                response.status_code,
                response.text[:5000],
            )

        response.raise_for_status()
        payload = response.json()

        if payload.get("success") is False:
            raise RuntimeError(
                f"Kaggle rejected the video job: "
                f"{payload.get('error') or payload}"
            )

        job_id = payload.get("job_id")
        if not job_id:
            raise RuntimeError(
                f"Kaggle video API did not return job_id: {payload}"
            )

        logger.info("Kaggle job queued: %s", job_id)
        return str(job_id)

    async def wait_for_video(
        self,
        client: httpx.AsyncClient,
        job_id: str,
    ) -> dict:

        timeout_seconds = max(
            900,
            int(settings.comfyui_request_timeout_seconds),
        )

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

                payload = response.json()
                failures = 0

            except (httpx.HTTPError, ValueError) as exc:
                failures += 1

                if failures > 30:
                    raise RuntimeError(
                        f"Lost connection to Kaggle while checking "
                        f"job {job_id}: {exc}"
                    ) from exc

                await asyncio.sleep(5)
                continue

            if payload.get("success") is False:
                raise RuntimeError(
                    f"Kaggle status request failed: "
                    f"{payload.get('error') or payload}"
                )

            job = payload.get("job")

            if not isinstance(job, dict):
                raise RuntimeError(
                    f"Unexpected Kaggle job-status response: {payload}"
                )

            state = str(job.get("status") or "").lower()

            logger.info(
                "Kaggle job %s: status=%s stage=%s progress=%s",
                job_id,
                state,
                job.get("stage"),
                job.get("progress"),
            )

            if state in {"completed", "done", "success"}:
                return job

            if state in {"failed", "error", "cancelled", "canceled"}:
                raise RuntimeError(
                    f"Kaggle video generation failed: "
                    f"{job.get('error') or job}"
                )

            await asyncio.sleep(5)

        raise TimeoutError(
            f"Kaggle video job {job_id} did not finish "
            f"within {timeout_seconds} seconds."
        )

    async def download_video(
        self,
        client: httpx.AsyncClient,
        job_id: str,
        destination: Path,
    ) -> None:

        outputs_response = await client.get(
            f"{self.base}/jobs/{job_id}/outputs",
            headers=self.headers,
            timeout=60.0,
        )

        outputs_response.raise_for_status()
        payload = outputs_response.json()

        if payload.get("success") is False:
            raise RuntimeError(
                f"Could not retrieve Kaggle job outputs: "
                f"{payload.get('error') or payload}"
            )

        outputs = payload.get("outputs") or []

        if not outputs:
            raise RuntimeError(
                f"Kaggle job {job_id} completed but returned no output files."
            )

        matching = [
            item for item in outputs
            if str(item.get("node_id")) == VIDEO_OUTPUT_NODE
            and item.get("download_url")
        ]

        if not matching:
            matching = [
                item for item in outputs
                if item.get("download_url")
            ]

        if not matching:
            raise RuntimeError(
                f"No downloadable video output was returned for job {job_id}."
            )

        output_record = matching[0]
        download_url = urljoin(
            f"{self.base}/",
            str(output_record["download_url"]),
        )

        destination.parent.mkdir(parents=True, exist_ok=True)

        temp = destination.with_suffix(destination.suffix + ".tmp")
        temp.unlink(missing_ok=True)

        try:
            async with client.stream(
                "GET",
                download_url,
                headers=self.headers,
                timeout=httpx.Timeout(
                    connect=30.0,
                    read=600.0,
                    write=30.0,
                    pool=30.0,
                ),
            ) as response:

                response.raise_for_status()
                written = 0

                with temp.open("wb") as output:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        if chunk:
                            output.write(chunk)
                            written += len(chunk)

            if written < 1024:
                raise RuntimeError(
                    f"Kaggle returned an unexpectedly small video "
                    f"({written} bytes)."
                )

            if not _valid_video(temp):
                raise RuntimeError(
                    "Kaggle output is not a valid MP4 video."
                )

            temp.replace(destination)

            logger.info(
                "Video downloaded: %s (%s bytes)",
                destination,
                written,
            )

        except Exception:
            temp.unlink(missing_ok=True)
            raise


def _scene_prompt(scene: dict, screenplay: dict) -> str:
    prompt = str(scene.get("video_prompt") or "").strip()

    if not prompt:
        prompt = str(
            scene.get("image_prompt")
            or scene.get("summary")
            or ""
        ).strip()

    identity = (
        "Use the supplied scene image as the exact first-frame visual reference. "
        "Preserve character identity, clothing, props, location, framing, lighting, "
        "and visual continuity. Animate only the actions described for this scene. "
        "Keep camera movement restrained unless the scene prompt explicitly requests it. "
        "The supplied audio is the exact soundtrack and must remain synchronized."
    )

    return (
        f"{identity}\n\n"
        f"FILM: {screenplay.get('title', '')}\n"
        f"SCENE: {scene.get('scene_id', '')}\n"
        f"{prompt}"
    )


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wav_file:
        rate = wav_file.getframerate()

        if rate <= 0:
            raise ValueError(f"Invalid WAV sample rate: {path}")

        return wav_file.getnframes() / float(rate)


def _scene_records(
    manifest: dict,
    scenes: list[dict],
) -> dict[str, dict]:

    if not isinstance(manifest.get("scenes"), list):
        manifest["scenes"] = []

    records = {
        str(item.get("scene_id")): item
        for item in manifest["scenes"]
        if isinstance(item, dict) and item.get("scene_id") is not None
    }

    ordered = []

    for index, scene in enumerate(scenes, start=1):
        scene_id = str(
            scene.get("scene_id") or f"SCENE_{index:03d}"
        )

        record = records.get(scene_id)

        if record is None:
            record = {
                "scene_id": scene_id,
                "status": "pending",
                "video": f"video/{_safe(scene_id)}.mp4",
            }

        record.setdefault("video", f"video/{_safe(scene_id)}.mp4")
        record.setdefault("status", "pending")

        ordered.append(record)
        records[scene_id] = record

    manifest["scenes"] = ordered
    return records


def _unfinished_scenes(
    project_path: Path,
    manifest: dict,
) -> list[dict]:
    unfinished = []

    for item in manifest.get("scenes", []):
        try:
            video_path = _safe_media_path(
                project_path,
                item.get("video", ""),
            )
        except (RuntimeError, TypeError, ValueError):
            unfinished.append(item)
            continue

        if item.get("status") != "completed" or not _valid_video(video_path):
            unfinished.append(item)

    return unfinished


async def _run_worker(
    project_id: int,
    project_path: Path,
    screenplay: dict,
    only_scene_id: str | None = None,
) -> None:

    project_path = Path(project_path)
    manifest = read_manifest(project_path)

    if not manifest:
        _set_stage(
            project_id,
            StageStatus.failed,
            "Video manifest is missing or unreadable.",
        )
        return

    _CONTROLS.pop(project_id, None)

    manifest["status"] = "running"
    manifest["error"] = None
    manifest["detail"] = (
        f"Preparing retry for {only_scene_id}..."
        if only_scene_id
        else "Connecting to Kaggle ComfyUI..."
    )
    manifest["current"] = None

    write_manifest(project_path, manifest)
    _set_stage(project_id, StageStatus.running, manifest["detail"])

    try:
        image_manifest_path = project_path / "image_manifest.json"
        audio_manifest_path = project_path / "audio" / "audio_manifest.json"

        if not image_manifest_path.is_file():
            raise RuntimeError("Scene image manifest is missing.")

        if not audio_manifest_path.is_file():
            raise RuntimeError("Scene audio manifest is missing.")

        image_manifest = json.loads(
            image_manifest_path.read_text(encoding="utf-8")
        )
        audio_manifest = json.loads(
            audio_manifest_path.read_text(encoding="utf-8")
        )

        image_scenes = image_manifest.get("scenes") or {}
        audio_scenes = {
            str(item.get("scene_id")): item
            for item in (audio_manifest.get("scenes") or [])
        }

        scenes = screenplay.get("scenes") or []

        if not scenes:
            raise RuntimeError("The screenplay contains no scenes.")

        manifest["total"] = len(scenes)
        records = _scene_records(manifest, scenes)

        if only_scene_id and only_scene_id not in records:
            raise ValueError(
                f"Scene does not exist in screenplay: {only_scene_id}"
            )

        manifest["completed"] = _completed_count(project_path, manifest)

        fps = int(settings.default_fps)
        client_config = KaggleComfyClient()

        timeout = httpx.Timeout(
            connect=30.0,
            read=max(
                900.0,
                float(settings.comfyui_request_timeout_seconds),
            ),
            write=300.0,
            pool=30.0,
        )

        failures = []

        async with httpx.AsyncClient(timeout=timeout) as client:

            for index, scene in enumerate(scenes, start=1):
                scene_id = str(
                    scene.get("scene_id") or f"SCENE_{index:03d}"
                )

                if only_scene_id and scene_id != only_scene_id:
                    continue

                record = records[scene_id]
                destination = _safe_media_path(
                    project_path,
                    record["video"],
                )

                if (
                    not only_scene_id
                    and record.get("status") == "completed"
                    and _valid_video(destination)
                ):
                    continue

                command = _CONTROLS.pop(project_id, None)

                if command in {"pause", "stop"}:
                    status = "paused" if command == "pause" else "stopped"

                    manifest.update(
                        status=status,
                        detail=f"{status.title()} at a safe scene boundary.",
                        current=None,
                    )

                    manifest["completed"] = _completed_count(
                        project_path, manifest
                    )

                    write_manifest(project_path, manifest)

                    _set_stage(
                        project_id,
                        StageStatus.paused
                        if command == "pause"
                        else StageStatus.stopped,
                        manifest["detail"],
                    )
                    return

                try:
                    image_record = image_scenes.get(scene_id) or {}
                    audio_record = audio_scenes.get(scene_id) or {}

                    image_rel = image_record.get("path")
                    audio_rel = audio_record.get("audio")

                    if not image_rel:
                        raise FileNotFoundError(
                            f"Image path is missing for {scene_id}."
                        )

                    if not audio_rel:
                        raise FileNotFoundError(
                            f"Audio path is missing for {scene_id}."
                        )

                    image_path = _safe_media_path(project_path, image_rel)
                    audio_path = _safe_media_path(project_path, audio_rel)

                    if not image_path.is_file():
                        raise FileNotFoundError(
                            f"Scene image is missing for {scene_id}: {image_rel}"
                        )

                    if not audio_path.is_file():
                        raise FileNotFoundError(
                            f"Scene audio is missing for {scene_id}: {audio_rel}"
                        )

                    audio_duration = _wav_duration(audio_path)

                    duration = float(
                        scene.get("duration_seconds")
                        or scene.get("duration")
                        or audio_duration
                    )
                    duration = max(1.0, duration)

                    manifest.update(
                        status="running",
                        current={
                            "index": index,
                            "total": len(scenes),
                            "scene_id": scene_id,
                        },
                        detail=f"Uploading inputs and queueing {scene_id} on Kaggle.",
                    )

                    record.update(
                        status="uploading",
                        duration_seconds=duration,
                        fps=fps,
                        error=None,
                    )

                    write_manifest(project_path, manifest)

                    job_id = await client_config.submit_job(
                        client=client,
                        image_path=image_path,
                        audio_path=audio_path,
                        prompt=_scene_prompt(scene, screenplay),
                        duration=duration,
                        fps=fps,
                    )

                    record.update(
                        status="running",
                        job_id=job_id,
                    )

                    manifest["detail"] = (
                        f"Generating {scene_id} on Kaggle (job {job_id})."
                    )
                    write_manifest(project_path, manifest)

                    await client_config.wait_for_video(client, job_id)

                    await client_config.download_video(
                        client,
                        job_id,
                        destination,
                    )

                    if not _valid_video(destination):
                        raise RuntimeError(
                            f"Downloaded video for {scene_id} is invalid."
                        )

                    record.update(
                        status="completed",
                        job_id=job_id,
                        size_bytes=destination.stat().st_size,
                    )
                    record.pop("error", None)

                    logger.info("Completed video scene %s", scene_id)

                except Exception as scene_exc:
                    logger.exception(
                        "Video scene %s failed for project %s",
                        scene_id,
                        project_id,
                    )

                    record["status"] = "failed"
                    record["error"] = str(scene_exc)
                    record["previous_video_available"] = _valid_video(
                        destination
                    )
                    failures.append(f"{scene_id}: {scene_exc}")

                    manifest["detail"] = (
                        f"{scene_id} failed. Continuing with the next scene."
                    )

                manifest["completed"] = _completed_count(
                    project_path, manifest
                )
                manifest["current"] = None
                write_manifest(project_path, manifest)

        manifest["completed"] = _completed_count(project_path, manifest)

        if only_scene_id:
            retried_record = records[only_scene_id]
            retried_path = _safe_media_path(
                project_path,
                retried_record.get("video", ""),
            )

            if (
                retried_record.get("status") == "completed"
                and _valid_video(retried_path)
            ):
                manifest["error"] = None
                manifest["detail"] = (
                    f"{only_scene_id} regenerated successfully. "
                    f"{manifest['completed']} / {manifest['total']} clips are valid."
                )

                unfinished = _unfinished_scenes(project_path, manifest)

                if not unfinished:
                    manifest["status"] = "completed"
                    manifest["detail"] = (
                        f"Generated all {manifest['total']} scene video clips."
                    )
                    _set_stage(
                        project_id,
                        StageStatus.completed,
                        manifest["detail"],
                    )
                else:
                    manifest["status"] = "paused"
                    _set_stage(
                        project_id,
                        StageStatus.paused,
                        manifest["detail"],
                    )
            else:
                manifest["status"] = "failed"
                manifest["error"] = retried_record.get(
                    "error", "Scene regeneration failed."
                )
                manifest["detail"] = (
                    f"Regeneration of {only_scene_id} failed. "
                    f"{manifest['completed']} / {manifest['total']} clips are valid."
                )
                _set_stage(
                    project_id,
                    StageStatus.failed,
                    manifest["detail"],
                )

        else:
            unfinished = _unfinished_scenes(project_path, manifest)

            if unfinished:
                manifest["status"] = "failed"
                manifest["error"] = (
                    f"{len(unfinished)} scene(s) still need attention."
                )
                manifest["detail"] = (
                    f"Video generation finished with "
                    f"{len(unfinished)} unfinished scene(s). "
                    f"{manifest['completed']} / {manifest['total']} clips are valid."
                )

                if failures:
                    manifest["detail"] += " " + "; ".join(failures[:3])

                _set_stage(
                    project_id,
                    StageStatus.failed,
                    manifest["detail"],
                )
            else:
                manifest["status"] = "completed"
                manifest["error"] = None
                manifest["detail"] = (
                    f"Generated all {manifest['total']} scene video clips."
                )
                _set_stage(
                    project_id,
                    StageStatus.completed,
                    manifest["detail"],
                )

        manifest["current"] = None
        write_manifest(project_path, manifest)

    except Exception as exc:
        logger.exception(
            "Video worker failed for project %s",
            project_id,
        )

        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        manifest["detail"] = f"Video generation failed: {exc}"
        manifest["current"] = None
        manifest["completed"] = _completed_count(project_path, manifest)

        write_manifest(project_path, manifest)

        _set_stage(
            project_id,
            StageStatus.failed,
            manifest["detail"],
        )

    finally:
        _TASKS.pop(project_id, None)
        _CONTROLS.pop(project_id, None)


def start_video_worker(
    project_id: int,
    project_path: Path,
    screenplay: dict,
    only_scene_id: str | None = None,
) -> bool:

    task = _TASKS.get(project_id)

    if task and not task.done():
        return False

    _TASKS[project_id] = asyncio.create_task(
        _run_worker(
            project_id,
            Path(project_path),
            screenplay,
            only_scene_id=only_scene_id,
        )
    )

    return True


def request_video_control(
    project_id: int,
    project_path: Path,
    command: str,
) -> str:

    if command not in {"pause", "stop"}:
        raise ValueError("Unsupported video control command")

    manifest = read_manifest(Path(project_path))

    if not manifest:
        raise ValueError("Video generation has not been prepared.")

    task = _TASKS.get(project_id)

    if task and not task.done():
        _CONTROLS[project_id] = command

        manifest["status"] = f"{command}_requested"
        manifest["detail"] = (
            f"{command.title()} requested; "
            "the current scene clip will finish first."
        )

        write_manifest(Path(project_path), manifest)
        return manifest["status"]

    if manifest.get("status") in {"paused", "stopped"}:
        return manifest["status"]

    raise ValueError(
        f"Video generation is not running "
        f"(status: {manifest.get('status')})."
    )