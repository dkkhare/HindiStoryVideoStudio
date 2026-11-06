from pathlib import Path
import json

FOLDERS = [
    "characters", "props", "locations", "scenes", "audio/narrator",
    "audio/characters", "final", "checkpoints", "logs", "references"
]

def create_project_workspace(base: Path, name: str) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    target = base / name
    suffix = 1
    while target.exists():
        target = base / f"{name}_{suffix:02d}"
        suffix += 1
    target.mkdir(parents=True)
    for folder in FOLDERS:
        (target / folder).mkdir(parents=True, exist_ok=True)
    (target / "project.json").write_text(
        json.dumps({"schema_version": 1, "project_name": target.name}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )
    return target
