from pathlib import Path
from app.services.project_files import create_project_workspace

def test_workspace_layout(tmp_path: Path):
    root = create_project_workspace(tmp_path, "Demo")
    assert (root / "characters").is_dir()
    assert (root / "scenes").is_dir()
    assert (root / "audio" / "narrator").is_dir()
    assert (root / "checkpoints").is_dir()
