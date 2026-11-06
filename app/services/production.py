from pathlib import Path
from sqlalchemy.orm import Session
from app.models import ProductionStage, StageStatus

class ProductionManager:
    """Persists stage transitions so interrupted projects can resume safely."""
    def __init__(self, db: Session, project_id: int, project_path: Path):
        self.db, self.project_id, self.project_path = db, project_id, project_path

    def set_stage(self, stage_key: str, status: StageStatus, detail: str | None = None):
        stage = self.db.query(ProductionStage).filter_by(
            project_id=self.project_id, stage_key=stage_key
        ).first()
        if stage is None:
            stage = ProductionStage(project_id=self.project_id, stage_key=stage_key)
            self.db.add(stage)
        stage.status, stage.detail = status, detail
        self.db.commit()

    def write_checkpoint(self, name: str, payload: dict):
        import json
        path = self.project_path / "checkpoints" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def read_checkpoint(self, name: str) -> dict | None:
        import json
        path = self.project_path / "checkpoints" / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
