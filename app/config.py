from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "Hindi Story Video Studio"
    app_env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    secret_key: str = "change-me"
    database_url: str = "sqlite:///./data/studio.db"
    projects_dir: Path = Path("./projects")
    logs_dir: Path = Path("./logs")

    gemini_api_key: str = ""
    gemini_text_model: str = "gemini-2.5-flash"
    gemini_image_model: str = "gemini-3.1-flash-image"
    gemini_tts_model: str = "gemini-2.5-flash-preview-tts"

    comfyui_base_url: str = "http://127.0.0.1:8188"
    comfyui_client_id: str = "hindi-story-studio"
    comfyui_workflow_json: Path = Path("./workflows/ltx25_i2v_api.json")
    comfyui_output_dir: str = ""

    f5tts_base_url: str = "http://127.0.0.1:7860"
    f5tts_api_path: str = "/api/predict"
    default_tts_provider: str = "f5tts"

    default_video_width: int = 1280
    default_video_height: int = 720
    default_fps: int = 24
    default_scene_seconds: int = 15
    max_parallel_image_jobs: int = 2
    max_parallel_audio_jobs: int = 2
    max_parallel_video_jobs: int = 1

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    def ensure_directories(self) -> None:
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        Path("./data").mkdir(parents=True, exist_ok=True)

settings = Settings()
