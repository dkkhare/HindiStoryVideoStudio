import enum
from datetime import datetime, timezone
from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base

def utcnow():
    return datetime.now(timezone.utc)

class ProjectStatus(str, enum.Enum):
    draft = "draft"
    planning = "planning"
    producing = "producing"
    completed = "completed"
    failed = "failed"

class StageStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    waiting_approval = "waiting_approval"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"

class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    story_idea: Mapped[str] = mapped_column(Text, nullable=False)
    target_duration_minutes: Mapped[int] = mapped_column(Integer, default=5)
    language: Mapped[str] = mapped_column(String(20), default="Hindi")
    visual_style: Mapped[str] = mapped_column(String(100), default="3D animated film")
    aspect_ratio: Mapped[str] = mapped_column(String(10), default="16:9")
    status: Mapped[ProjectStatus] = mapped_column(Enum(ProjectStatus), default=ProjectStatus.draft)
    project_path: Mapped[str] = mapped_column(String(500), nullable=False)
    screenplay_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    stages: Mapped[list["ProductionStage"]] = relationship(back_populates="project", cascade="all, delete-orphan")

class ProductionStage(Base):
    __tablename__ = "production_stages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    stage_key: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[StageStatus] = mapped_column(Enum(StageStatus), default=StageStatus.pending)
    detail: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    project: Mapped[Project] = relationship(back_populates="stages")
