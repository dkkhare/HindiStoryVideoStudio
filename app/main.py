from contextlib import asynccontextmanager
import asyncio
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from app.config import settings
from app.database import init_db
from app.api.routes import router
from app.logging_config import configure_logging
from app.services.kaggle_workers import health_monitor

configure_logging()

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_directories()
    init_db()
    monitor_task = asyncio.create_task(health_monitor())
    try:
        yield
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.include_router(router, prefix="/api")

@app.get("/", include_in_schema=False)
async def home():
    return RedirectResponse(url="/api/studio")

@app.get("/health")
async def health():
    return {"status": "ok", "app": settings.app_name}

app.mount("/static", StaticFiles(directory="app/static"), name="static")
