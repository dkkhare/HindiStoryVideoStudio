from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from app.config import settings
from app.database import init_db
from app.api.routes import router
from app.logging_config import configure_logging

configure_logging()

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_directories()
    init_db()
    yield

app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.include_router(router, prefix="/api")

@app.get("/", include_in_schema=False)
async def home():
    return RedirectResponse(url="/api/studio")

@app.get("/health")
async def health():
    return {"status": "ok", "app": settings.app_name}

app.mount("/static", StaticFiles(directory="app/static"), name="static")
