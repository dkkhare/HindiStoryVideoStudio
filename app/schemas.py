from datetime import datetime
from pydantic import BaseModel, Field
from app.models import ProjectStatus, StageStatus

class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    story_idea: str = Field(min_length=10)
    target_duration_minutes: int = Field(default=5, ge=1, le=180)
    language: str = Field(default="Hindi", min_length=1, max_length=20)
    visual_style: str = "Photorealistic live-action cinematic"
    aspect_ratio: str = "16:9"

class StageOut(BaseModel):
    stage_key: str
    status: StageStatus
    detail: str | None = None
    class Config:
        from_attributes = True

class ProjectOut(BaseModel):
    id: int
    title: str
    story_idea: str
    target_duration_minutes: int
    language: str
    visual_style: str
    aspect_ratio: str
    status: ProjectStatus
    project_path: str
    created_at: datetime
    class Config:
        from_attributes = True

class ScreenplayResponse(BaseModel):
    project_id: int
    screenplay: dict
    saved_to: str


class AudioGenerateRequest(BaseModel):
    profile_id: str = Field(min_length=1)
    engine: str = Field(default="chatterbox", pattern="^(qwen|qwen_custom_voice|luxtts|chatterbox|chatterbox_turbo|tada|kokoro)$")
