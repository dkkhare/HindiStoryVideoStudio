"""Direct ComfyUI API video rendering (no Kaggle /jobs worker required)."""
import asyncio
import copy
import json
import logging
import mimetypes
import uuid
import wave
from pathlib import Path
from urllib.parse import quote

import httpx

from app.config import settings
from app.database import SessionLocal
from app.models import Project, ProductionStage, StageStatus
from app.services.kaggle_workers import decrypt_api_key

logger = logging.getLogger(__name__)
_TASKS: dict[int, asyncio.Task] = {}
_CONTROLS: dict[int, str] = {}


def manifest_path(project_path: Path) -> Path:
    return Path(project_path) / "video" / "video_manifest.json"


def read_manifest(project_path: Path) -> dict | None:
    path = manifest_path(project_path)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not read video manifest: %s", path)
        return None


def write_manifest(project_path: Path, manifest: dict) -> None:
    path = manifest_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _safe_media_path(root: Path, relative: str) -> Path:
    base = Path(root).resolve()
    target = (base / str(relative)).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise RuntimeError(f"Invalid media path: {relative}") from exc
    return target


def _valid_video(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < 1024:
            return False
        with path.open("rb") as stream:
            return b"ftyp" in stream.read(64)
    except OSError:
        return False


def _completed_count(root: Path, manifest: dict) -> int:
    total = 0
    for item in manifest.get("scenes", []):
        try:
            if item.get("status") == "completed" and _valid_video(_safe_media_path(root, item.get("video", ""))):
                total += 1
        except (RuntimeError, TypeError, ValueError):
            pass
    return total


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


def _get_config(project_id: int) -> dict:
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise RuntimeError(f"Project {project_id} no longer exists.")
        saved = json.loads(project.provider_config_json or "{}")
        item = saved.get("comfy_video") or {}
        api_url = str(item.get("api_url") or "").strip().rstrip("/")
        encrypted = item.get("encrypted_api_key") or ""
        api_key = decrypt_api_key(encrypted) if encrypted else ""
        workflow = item.get("workflow_json") or ""
        if isinstance(workflow, str):
            workflow = json.loads(workflow) if workflow.strip() else {}
        if not api_url:
            raise RuntimeError("Configure the ComfyUI API Base URL in Project API Settings > Direct ComfyUI Video.")
        if not isinstance(workflow, dict) or not workflow:
            raise RuntimeError("Upload the ComfyUI API-exported workflow JSON in Project API Settings > Direct ComfyUI Video before rendering.")
        return {"api_url": api_url, "api_key": api_key, "workflow": workflow}
    finally:
        db.close()


class ComfyUIClient:
    def __init__(self, config: dict):
        self.base = config["api_url"].rstrip("/")
        self.api_key = config.get("api_key") or ""
        self.headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        self.client_id = str(uuid.uuid4())

    async def upload(self, client: httpx.AsyncClient, path: Path) -> dict:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as stream:
            response = await client.post(
                f"{self.base}/upload/image",
                headers=self.headers,
                data={"type": "input", "overwrite": "true"},
                files={"image": (path.name, stream, mime)},
                timeout=300.0,
            )
        response.raise_for_status()
        data = response.json()
        if not data.get("name"):
            raise RuntimeError(f"ComfyUI upload returned no filename: {data}")
        return data

    @staticmethod
    def _set_input(workflow: dict, node_id: str, input_name: str, value) -> None:
        workflow[str(node_id)]["inputs"][input_name] = value

    def build_workflow(self, template: dict, uploads: list[dict], prompt: str, duration: float, fps: int) -> dict:
        workflow = copy.deepcopy(template)
        nodes = [(str(k), v) for k, v in workflow.items() if isinstance(v, dict) and isinstance(v.get("inputs"), dict)]
        image_nodes = [(nid, node) for nid, node in nodes if "loadimage" in str(node.get("class_type", "")).lower()]
        audio_nodes = [(nid, node) for nid, node in nodes if "loadaudio" in str(node.get("class_type", "")).lower()]
        if not image_nodes:
            raise RuntimeError("The exported workflow has no LoadImage node. Keep the main image input enabled before exporting API workflow.")
        if not audio_nodes:
            raise RuntimeError("The exported workflow has no LoadAudio node. Keep the audio input enabled before exporting API workflow.")
        for index, (node_id, node) in enumerate(image_nodes):
            upload = uploads[min(index, len(uploads) - 1)]
            key = next((k for k in node["inputs"] if k.lower() in {"image", "filename"}), None)
            if key is None:
                key = next((k for k, v in node["inputs"].items() if isinstance(v, str)), None)
            if key is not None:
                self._set_input(workflow, node_id, key, upload["name"])
        audio_node_id, audio_node = audio_nodes[0]
        audio_key = next((k for k in audio_node["inputs"] if k.lower() in {"audio", "filename"}), None)
        if audio_key is None:
            audio_key = next((k for k, v in audio_node["inputs"].items() if isinstance(v, str)), None)
        if audio_key is None:
            raise RuntimeError("Could not identify the audio filename input on the LoadAudio node.")
        self._set_input(workflow, audio_node_id, audio_key, self._audio_upload_name)
        prompt_candidates = []
        for node_id, node in nodes:
            for key, value in node["inputs"].items():
                if isinstance(value, str) and "negative" not in key.lower() and ("prompt" in key.lower() or key.lower() in {"text", "positive"}):
                    prompt_candidates.append((node_id, key))
        if prompt_candidates:
            # Use the most explicit prompt-labelled input; never overwrite connected inputs.
            chosen = next((item for item in prompt_candidates if "prompt" in item[1].lower()), prompt_candidates[0])
            self._set_input(workflow, chosen[0], chosen[1], prompt)
        for node_id, node in nodes:
            for key, value in node["inputs"].items():
                key_lower = key.lower()
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if "fps" in key_lower or "frame_rate" in key_lower:
                        self._set_input(workflow, node_id, key, int(fps))
                    elif "duration" in key_lower or "seconds" in key_lower or "length_sec" in key_lower:
                        self._set_input(workflow, node_id, key, float(duration))
        return workflow

    async def queue(self, client: httpx.AsyncClient, workflow: dict) -> str:
        response = await client.post(
            f"{self.base}/prompt",
            headers=self.headers,
            json={"prompt": workflow, "client_id": self.client_id, "extra_data": {}},
            timeout=120.0,
        )
        if response.is_error:
            raise RuntimeError(f"ComfyUI /prompt returned HTTP {response.status_code}: {response.text[:5000]}")
        data = response.json()
        if data.get("node_errors"):
            raise RuntimeError(f"ComfyUI workflow validation failed: {data['node_errors']}")
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"ComfyUI did not return prompt_id: {data}")
        return str(prompt_id)

    async def wait(self, client: httpx.AsyncClient, prompt_id: str, project_id: int) -> dict:
        timeout = max(900, int(settings.comfyui_request_timeout_seconds))
        deadline = asyncio.get_running_loop().time() + timeout
        failures = 0
        while asyncio.get_running_loop().time() < deadline:
            if _CONTROLS.get(project_id) in {"pause", "stop"}:
                try:
                    await client.post(f"{self.base}/interrupt", headers=self.headers, json={"prompt_id": prompt_id}, timeout=20.0)
                except httpx.HTTPError:
                    logger.warning("Could not interrupt ComfyUI prompt %s", prompt_id)
                raise InterruptedError(_CONTROLS.get(project_id, "pause"))
            try:
                response = await client.get(f"{self.base}/history/{quote(prompt_id)}", headers=self.headers, timeout=60.0)
                response.raise_for_status()
                payload = response.json()
                failures = 0
            except (httpx.HTTPError, ValueError) as exc:
                failures += 1
                if failures > 30:
                    raise RuntimeError(f"Lost connection while checking ComfyUI prompt {prompt_id}: {exc}") from exc
                await asyncio.sleep(3)
                continue
            record = payload.get(prompt_id) or payload.get(str(prompt_id))
            if record:
                status = record.get("status") or {}
                status_text = str(status.get("status_str") or "").lower()
                messages = json.dumps(status.get("messages") or [])
                if status_text in {"error", "failed"} or "execution_error" in messages:
                    raise RuntimeError(f"ComfyUI execution failed: {messages[:4000]}")
                if status.get("completed") or status_text in {"success", "completed"}:
                    return record
            await asyncio.sleep(3)
        raise TimeoutError(f"ComfyUI prompt {prompt_id} exceeded {timeout} seconds.")

    async def download(self, client: httpx.AsyncClient, history: dict, destination: Path) -> None:
        candidates = []
        for node_output in (history.get("outputs") or {}).values():
            for field in ("videos", "gifs", "files"):
                for item in node_output.get(field) or []:
                    if str(item.get("filename") or "").lower().endswith((".mp4", ".webm", ".mov", ".mkv")):
                        candidates.append(item)
        if not candidates:
            raise RuntimeError("ComfyUI job completed, but history contains no video output. Check that the workflow ends with a video save/output node.")
        output = candidates[0]
        params = {"filename": output["filename"], "subfolder": output.get("subfolder", ""), "type": output.get("type", "output")}
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_suffix(destination.suffix + ".tmp")
        temp.unlink(missing_ok=True)
        try:
            async with client.stream("GET", f"{self.base}/view", headers=self.headers, params=params, timeout=httpx.Timeout(connect=30, read=900, write=30, pool=30)) as response:
                response.raise_for_status()
                with temp.open("wb") as stream:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        stream.write(chunk)
            if not _valid_video(temp):
                raise RuntimeError("Downloaded ComfyUI output is not a valid MP4-compatible video.")
            temp.replace(destination)
        except Exception:
            temp.unlink(missing_ok=True)
            raise


def _scene_prompt(scene: dict, screenplay: dict) -> str:
    prompt = str(scene.get("video_prompt") or scene.get("image_prompt") or scene.get("summary") or "").strip()
    return (
        "Use the supplied scene image as the exact first-frame visual reference. Preserve character identity, clothing, props, location, framing, lighting and visual continuity. Animate only the actions described. Keep camera movement restrained unless explicitly requested. The supplied audio is the exact soundtrack and must remain synchronized.\n\n"
        f"FILM: {screenplay.get('title', '')}\nSCENE: {scene.get('scene_id', '')}\n{prompt}"
    )


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        if audio.getframerate() <= 0 or audio.getnframes() <= 0:
            raise ValueError(f"Invalid WAV audio: {path}")
        return audio.getnframes() / float(audio.getframerate())


def _scene_records(manifest: dict, scenes: list[dict]) -> dict[str, dict]:
    existing = {str(item.get("scene_id")): item for item in manifest.get("scenes", []) if isinstance(item, dict)}
    ordered = []
    for index, scene in enumerate(scenes, 1):
        scene_id = str(scene.get("scene_id") or f"SCENE_{index:03d}")
        record = existing.get(scene_id) or {"scene_id": scene_id, "status": "pending", "video": f"video/{scene_id}.mp4"}
        record.setdefault("video", f"video/{scene_id}.mp4")
        record.setdefault("status", "pending")
        ordered.append(record)
    manifest["scenes"] = ordered
    return {str(item["scene_id"]): item for item in ordered}


async def _run_worker(project_id: int, root: Path, screenplay: dict, only_scene_id: str | None = None) -> None:
    manifest = read_manifest(root)
    if not manifest:
        _set_stage(project_id, StageStatus.failed, "Video manifest is missing or unreadable.")
        return
    _CONTROLS.pop(project_id, None)
    manifest.update(status="running", error=None, detail="Connecting to direct ComfyUI API.", current=None)
    write_manifest(root, manifest)
    _set_stage(project_id, StageStatus.running, manifest["detail"])
    try:
        config = _get_config(project_id)
        image_manifest = json.loads((root / "image_manifest.json").read_text(encoding="utf-8"))
        audio_manifest = json.loads((root / "audio" / "audio_manifest.json").read_text(encoding="utf-8"))
        image_scenes = image_manifest.get("scenes") or {}
        audio_scenes = {str(item.get("scene_id")): item for item in audio_manifest.get("scenes") or []}
        scenes = screenplay.get("scenes") or []
        records = _scene_records(manifest, scenes)
        manifest["total"] = len(scenes)
        manifest["completed"] = _completed_count(root, manifest)
        timeout = httpx.Timeout(connect=30, read=max(900, int(settings.comfyui_request_timeout_seconds)), write=300, pool=30)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for index, scene in enumerate(scenes, 1):
                scene_id = str(scene.get("scene_id") or f"SCENE_{index:03d}")
                if only_scene_id and scene_id != only_scene_id:
                    continue
                record = records[scene_id]
                destination = _safe_media_path(root, record["video"])
                if not only_scene_id and record.get("status") == "completed" and _valid_video(destination):
                    continue
                if _CONTROLS.get(project_id) in {"pause", "stop"}:
                    break
                manifest.update(current={"index": index, "total": len(scenes), "scene_id": scene_id}, detail=f"Preparing {scene_id}.")
                record.update(status="uploading", error=None)
                write_manifest(root, manifest)
                image_record = image_scenes.get(scene_id) or {}
                audio_record = audio_scenes.get(scene_id) or {}
                image_rel, audio_rel = image_record.get("path"), audio_record.get("audio")
                if not image_rel or not audio_rel:
                    raise RuntimeError(f"Image or audio manifest entry is missing for {scene_id}.")
                image_path, audio_path = _safe_media_path(root, image_rel), _safe_media_path(root, audio_rel)
                if not image_path.is_file() or not audio_path.is_file():
                    raise FileNotFoundError(f"Scene image/audio file is missing for {scene_id}.")
                _wav_duration(audio_path)
                comfy = ComfyUIClient(config)
                uploads = [await comfy.upload(client, image_path)]
                # Optional scene reference images can be supplied in the image manifest as extra_images.
                extras = image_record.get("extra_images") or []
                for extra in extras:
                    extra_path = _safe_media_path(root, extra if isinstance(extra, str) else extra.get("path", ""))
                    if extra_path.is_file():
                        uploads.append(await comfy.upload(client, extra_path))
                audio_upload = await comfy.upload(client, audio_path)
                comfy._audio_upload_name = audio_upload["name"]
                duration = max(1.0, float(scene.get("duration_seconds") or scene.get("duration") or _wav_duration(audio_path)))
                workflow = comfy.build_workflow(config["workflow"], uploads, _scene_prompt(scene, screenplay), duration, int(settings.default_fps))
                prompt_id = await comfy.queue(client, workflow)
                record.update(status="running", prompt_id=prompt_id)
                manifest["detail"] = f"Generating {scene_id} in ComfyUI (prompt {prompt_id})."
                write_manifest(root, manifest)
                history = await comfy.wait(client, prompt_id, project_id)
                await comfy.download(client, history, destination)
                record.update(status="completed", size_bytes=destination.stat().st_size)
                record.pop("error", None)
                manifest["completed"] = _completed_count(root, manifest)
                write_manifest(root, manifest)
                logger.info("ComfyUI video completed for project %s scene %s", project_id, scene_id)
        command = _CONTROLS.pop(project_id, None)
        manifest["completed"] = _completed_count(root, manifest)
        if command in {"pause", "stop"}:
            manifest.update(status="paused" if command == "pause" else "stopped", detail=f"{command.title()} after the active ComfyUI job was interrupted.", current=None)
            _set_stage(project_id, StageStatus.paused if command == "pause" else StageStatus.stopped, manifest["detail"])
        else:
            unfinished = [item for item in manifest.get("scenes", []) if item.get("status") != "completed" or not _valid_video(_safe_media_path(root, item.get("video", "")))]
            if unfinished:
                manifest.update(status="failed", error=f"{len(unfinished)} scene(s) still need attention.", detail=f"{manifest['completed']} / {manifest['total']} clips completed.")
                _set_stage(project_id, StageStatus.failed, manifest["detail"])
            else:
                manifest.update(status="completed", error=None, detail=f"Generated all {manifest['total']} scene video clips.")
                _set_stage(project_id, StageStatus.completed, manifest["detail"])
        manifest["current"] = None
        write_manifest(root, manifest)
    except InterruptedError as exc:
        command = str(exc)
        manifest.update(status="paused" if command == "pause" else "stopped", detail=f"{command.title()} requested; ComfyUI execution interrupted.", current=None)
        manifest["completed"] = _completed_count(root, manifest)
        write_manifest(root, manifest)
        _set_stage(project_id, StageStatus.paused if command == "pause" else StageStatus.stopped, manifest["detail"])
    except Exception as exc:
        logger.exception("Direct ComfyUI video worker failed for project %s", project_id)
        manifest.update(status="failed", error=str(exc), detail=f"Video generation failed: {exc}", current=None)
        manifest["completed"] = _completed_count(root, manifest)
        write_manifest(root, manifest)
        _set_stage(project_id, StageStatus.failed, manifest["detail"])
    finally:
        _TASKS.pop(project_id, None)
        _CONTROLS.pop(project_id, None)


def start_video_worker(project_id: int, project_path: Path, screenplay: dict, only_scene_id: str | None = None) -> bool:
    task = _TASKS.get(project_id)
    if task and not task.done():
        return False
    _TASKS[project_id] = asyncio.create_task(_run_worker(project_id, Path(project_path), screenplay, only_scene_id))
    return True


def video_worker_active(project_id: int) -> bool:
    task = _TASKS.get(project_id)
    return bool(task and not task.done())


def request_video_control(project_id: int, project_path: Path, command: str) -> str:
    if command not in {"pause", "stop"}:
        raise ValueError("Unsupported video control command.")
    manifest = read_manifest(Path(project_path))
    if not manifest:
        raise ValueError("Video generation has not been prepared.")
    if video_worker_active(project_id):
        _CONTROLS[project_id] = command
        manifest.update(status=f"{command}_requested", detail=f"{command.title()} requested; the active ComfyUI job will be interrupted.")
        write_manifest(Path(project_path), manifest)
        return manifest["status"]
    if manifest.get("status") in {"paused", "stopped"}:
        return manifest["status"]
    raise ValueError(f"Video generation is not running (status: {manifest.get('status')}).")
