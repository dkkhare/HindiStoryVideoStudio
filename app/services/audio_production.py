import asyncio
import json
import logging
import wave
from pathlib import Path
from typing import Any

from app.database import SessionLocal
from app.models import ProductionStage, StageStatus
from app.services.tts import get_tts_provider

logger = logging.getLogger(__name__)
_TASKS: dict[int, asyncio.Task] = {}
_CONTROLS: dict[int, str] = {}


def _safe(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(value))


def _manifest_path(project_path: Path) -> Path:
    return project_path / "audio" / "audio_manifest.json"


def read_manifest(project_path: Path) -> dict[str, Any] | None:
    path = _manifest_path(project_path)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_manifest(project_path: Path, manifest: dict[str, Any]) -> None:
    path = _manifest_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _set_stage(project_id: int, status: StageStatus, detail: str) -> None:
    db = SessionLocal()
    try:
        stage = db.query(ProductionStage).filter_by(project_id=project_id, stage_key="audio").first()
        if stage:
            stage.status = status
            stage.detail = detail
            db.commit()
    finally:
        db.close()


def _valid_wav(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as wav:
            return wav.getnframes() > 0
    except (OSError, wave.Error):
        return False


def _combine_scene_wavs(paths: list[Path], output: Path) -> bool:
    if not paths:
        return False
    try:
        with wave.open(str(paths[0]), "rb") as first:
            params = first.getparams()
            chunks = [first.readframes(first.getnframes())]
        for path in paths[1:]:
            with wave.open(str(path), "rb") as source:
                if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != (
                    params.nchannels, params.sampwidth, params.framerate, params.comptype
                ):
                    return False
                chunks.append(source.readframes(source.getnframes()))
        silence = bytes(int(params.nchannels * params.sampwidth * params.framerate * 0.25))
        output.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output), "wb") as target:
            target.setparams(params)
            for index, chunk in enumerate(chunks):
                if index:
                    target.writeframes(silence)
                target.writeframes(chunk)
        return True
    except (OSError, wave.Error):
        logger.exception("Could not combine scene audio: %s", output)
        return False


def required_speakers(screenplay: dict) -> list[dict[str, str]]:
    characters = screenplay.get("characters") or []
    result = [
        {
            "id": str(item.get("character_id") or item.get("id") or ""),
            "name": str(item.get("name") or item.get("character_id") or "Character"),
            "kind": "character",
        }
        for item in characters
        if item.get("character_id") or item.get("id")
    ]
    result.append({"id": "NARRATOR", "name": "Narrator / Voice-over", "kind": "narrator"})
    return result


def _flatten(screenplay: dict) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    speaker_lookup = {}
    for character in screenplay.get("characters") or []:
        character_id = str(character.get("character_id") or character.get("id") or "").strip()
        character_name = str(character.get("name") or "").strip()
        if character_id:
            speaker_lookup[character_id.casefold()] = character_id
        if character_name and character_id:
            speaker_lookup[character_name.casefold()] = character_id

    for scene_index, scene in enumerate(screenplay.get("scenes") or [], start=1):
        scene_id = str(scene.get("scene_id") or f"SCENE_{scene_index:03d}")
        for segment_index, segment in enumerate(scene.get("dialogue_and_narration") or [], start=1):
            text = str(segment.get("text") or "").strip()
            if not text:
                continue
            raw_speaker = str(segment.get("speaker") or "NARRATOR").strip() or "NARRATOR"
            if raw_speaker.casefold() in {"narrator", "voiceover", "voice-over", "voice over"}:
                speaker = "NARRATOR"
            else:
                speaker = speaker_lookup.get(raw_speaker.casefold(), raw_speaker)
            segment_id = str(segment.get("segment_id") or f"SEG_{segment_index:03d}")
            filename = f"{_safe(segment_id)}_{_safe(speaker)}.wav"
            relative = f"audio/{_safe(scene_id)}/{filename}"
            items.append({
                "scene_id": scene_id,
                "segment_id": segment_id,
                "speaker": speaker,
                "kind": str(segment.get("kind") or "dialogue"),
                "text": text,
                "audio": relative,
                "status": "pending",
            })
    return items


async def _run_worker(
    project_id: int,
    project_path: Path,
    language: str,
    engine: str,
) -> None:
    manifest = read_manifest(project_path)
    if not manifest:
        return
    _CONTROLS.pop(project_id, None)
    manifest["status"] = "running"
    manifest["error"] = None
    write_manifest(project_path, manifest)
    _set_stage(project_id, StageStatus.running, "Audio generation started/resumed.")

    provider_name = str(manifest.get("provider") or "voicebox").lower()
    provider = get_tts_provider(provider_name)
    segments = manifest.get("segments") or []
    assignments = manifest.get("voice_assignments") or []
    assignment_map = {item["speaker_id"]: item["profile_id"] for item in assignments}
    total = len(segments)

    try:
        for index, item in enumerate(segments):
            command = _CONTROLS.pop(project_id, None)
            if command in {"pause", "stop"}:
                manifest["status"] = "paused" if command == "pause" else "stopped"
                manifest["detail"] = (
                    "Paused at a safe segment boundary."
                    if command == "pause"
                    else "Stopped at a safe segment boundary. Completed audio is preserved."
                )
                manifest["current"] = None
                write_manifest(project_path, manifest)
                _set_stage(
                    project_id,
                    StageStatus.paused if command == "pause" else StageStatus.stopped,
                    manifest["detail"],
                )
                return
            path = project_path / item["audio"]
            if item.get("status") == "completed" and _valid_wav(path):
                continue
            # Recover safely if the WAV was written but the process stopped before
            # the manifest checkpoint was committed.
            if _valid_wav(path):
                item["status"] = "completed"
                write_manifest(project_path, manifest)
            else:
                item["status"] = "generating"
                manifest["current"] = {
                    "index": index + 1,
                    "total": total,
                    "scene_id": item["scene_id"],
                    "segment_id": item["segment_id"],
                    "speaker": item["speaker"],
                }
                manifest["completed"] = sum(x.get("status") == "completed" for x in segments)
                write_manifest(project_path, manifest)
                profile_id = assignment_map.get(item["speaker"])
                if not profile_id:
                    raise ValueError(f"No voice profile assigned for speaker {item['speaker']}")
                audio_bytes = await provider.synthesize_bytes(
                    text=item["text"],
                    profile_id=profile_id,
                    language=language,
                    engine=engine,
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".wav.tmp")
                temporary.write_bytes(audio_bytes)
                if not _valid_wav(temporary):
                    temporary.unlink(missing_ok=True)
                    raise RuntimeError(f"{provider_name} returned invalid WAV audio for {item['segment_id']}")
                temporary.replace(path)
                item["status"] = "completed"
                item["profile_id"] = profile_id
                item.pop("error", None)

            manifest["completed"] = sum(x.get("status") == "completed" for x in segments)
            manifest["next_segment_index"] = next(
                (i for i, x in enumerate(segments) if x.get("status") != "completed"), len(segments)
            )
            manifest["current"] = None
            write_manifest(project_path, manifest)

            command = _CONTROLS.pop(project_id, None)
            if command in {"pause", "stop"}:
                manifest["status"] = "paused" if command == "pause" else "stopped"
                manifest["detail"] = (
                    "Paused after completing the current audio segment."
                    if command == "pause"
                    else "Stopped after completing the current audio segment. Completed audio is preserved."
                )
                write_manifest(project_path, manifest)
                stage_status = StageStatus.paused if command == "pause" else StageStatus.stopped
                _set_stage(project_id, stage_status, manifest["detail"])
                return

        # Rebuild scene-level WAVs from the individually checkpointed segments.
        scene_records = []
        scenes: dict[str, list[dict[str, Any]]] = {}
        for item in segments:
            scenes.setdefault(item["scene_id"], []).append(item)
        for scene_id, scene_items in scenes.items():
            wav_paths = [project_path / item["audio"] for item in scene_items if _valid_wav(project_path / item["audio"])]
            combined_rel = f"audio/{_safe(scene_id)}.wav"
            combined_ok = _combine_scene_wavs(wav_paths, project_path / combined_rel)
            scene_records.append({
                "scene_id": scene_id,
                "audio": combined_rel if combined_ok else None,
                "segments": scene_items,
            })

        manifest["scenes"] = scene_records
        manifest["status"] = "completed"
        manifest["completed"] = total
        manifest["next_segment_index"] = total
        manifest["current"] = None
        manifest["detail"] = f"Generated {total} audio segments across {len(scene_records)} scenes."
        write_manifest(project_path, manifest)
        _set_stage(project_id, StageStatus.completed, manifest["detail"])
    except Exception as exc:
        logger.exception("Audio generation failed for project %s", project_id)
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        manifest["detail"] = f"Audio generation failed: {exc}"
        manifest["current"] = None
        write_manifest(project_path, manifest)
        _set_stage(project_id, StageStatus.failed, manifest["detail"])
    finally:
        _TASKS.pop(project_id, None)
        _CONTROLS.pop(project_id, None)


def start_audio_worker(project_id: int, project_path: Path, language: str, engine: str) -> bool:
    task = _TASKS.get(project_id)
    if task and not task.done():
        return False
    _TASKS[project_id] = asyncio.create_task(
        _run_worker(project_id, Path(project_path), language, engine)
    )
    return True


def request_audio_control(project_id: int, project_path: Path, command: str) -> str:
    if command not in {"pause", "stop"}:
        raise ValueError("Unsupported audio control command")
    task = _TASKS.get(project_id)
    manifest = read_manifest(Path(project_path))
    if not manifest:
        raise ValueError("Audio generation has not been prepared")
    if manifest.get("status") not in {"running", "pause_requested", "stop_requested"}:
        if command == "pause" and manifest.get("status") == "paused":
            return "paused"
        if command == "stop" and manifest.get("status") == "stopped":
            return "stopped"
        raise ValueError(f"Audio generation is not running (status: {manifest.get('status')})")
    if task and not task.done():
        _CONTROLS[project_id] = command
        manifest["status"] = f"{command}_requested"
        manifest["detail"] = f"{command.title()} requested; finishing the current audio segment."
        write_manifest(Path(project_path), manifest)
        return manifest["status"]
    final_status = "paused" if command == "pause" else "stopped"
    manifest["status"] = final_status
    manifest["detail"] = "Worker was not active; checkpoint is safe to resume."
    write_manifest(Path(project_path), manifest)
    _set_stage(
        project_id,
        StageStatus.paused if command == "pause" else StageStatus.stopped,
        manifest["detail"],
    )
    return final_status


def audio_worker_active(project_id: int) -> bool:
    task = _TASKS.get(project_id)
    return bool(task and not task.done())
