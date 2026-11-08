"""Database-backed registry and scheduler for independent Kaggle workers."""
import asyncio
import hashlib
import ipaddress
import logging
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select, update, delete

from app.config import settings
from app.database import SessionLocal
from app.models import KaggleWorker

logger = logging.getLogger(__name__)
MAX_WORKERS = 5
OFFLINE_FAILURE_LIMIT = 3
HEALTH_INTERVAL_SECONDS = 60


def _fernet() -> Fernet:
    secret = str(settings.secret_key or "")
    if len(secret) < 32 or secret == "change-me":
        raise RuntimeError(
            "Set a unique SECRET_KEY (at least 32 characters) before registering Kaggle workers."
        )
    key = hashlib.sha256(secret.encode("utf-8")).digest()
    import base64
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_api_key(api_key: str) -> str:
    return _fernet().encrypt(api_key.encode("utf-8")).decode("ascii")


def decrypt_api_key(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError("Could not decrypt the stored Kaggle API key. Check SECRET_KEY.") from exc


def _validate_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("Enter a valid Kaggle gateway URL beginning with https://.")
    if parsed.username or parsed.password:
        raise ValueError("Do not put credentials in the URL; use the API key field.")
    if parsed.scheme != "https":
        host = parsed.hostname.lower()
        if host not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("HTTP is allowed only for localhost; use HTTPS for remote workers.")
    return url


async def register_worker(name: str, api_url: str, api_key: str) -> dict:
    name = name.strip()
    if not name or not api_key.strip():
        raise ValueError("Worker name and API key are required.")
    url = _validate_url(api_url)
    workflow_path = settings.comfyui_workflow_json
    if not workflow_path.is_file():
        raise RuntimeError(f"Workflow JSON file not found: {workflow_path}")
    workflow_json = workflow_path.read_text(encoding="utf-8")
    headers = {"X-API-Key": api_key.strip()}
    timeout = httpx.Timeout(connect=20, read=120, write=120, pool=20)
    async with httpx.AsyncClient(timeout=timeout) as client:
        health = await client.get(f"{url}/health")
        health.raise_for_status()
        response = await client.post(
            f"{url}/workflows/register",
            headers=headers,
            json={"workflow_json": workflow_json},
        )
        response.raise_for_status()
        payload = response.json()
    workflow_id = payload.get("workflow_id")
    if payload.get("success") is False or not workflow_id:
        raise RuntimeError(f"Workflow registration failed: {payload.get('error') or payload}")
    db = SessionLocal()
    try:
        if db.query(KaggleWorker).count() >= MAX_WORKERS:
            raise ValueError(f"Only {MAX_WORKERS} Kaggle workers can be registered.")
        duplicate = db.query(KaggleWorker).filter(KaggleWorker.api_url == url).first()
        if duplicate:
            raise ValueError("This Kaggle URL is already registered.")
        worker = KaggleWorker(
            name=name[:120],
            api_url=url,
            encrypted_api_key=encrypt_api_key(api_key.strip()),
            workflow_id=str(workflow_id),
            status="FREE",
            consecutive_health_failures=0,
            last_seen_at=datetime.now(timezone.utc),
        )
        db.add(worker)
        db.commit()
        db.refresh(worker)
        return serialize_worker(worker)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def serialize_worker(worker: KaggleWorker) -> dict:
    return {
        "id": worker.id,
        "name": worker.name,
        "api_url": worker.api_url,
        "workflow_id": worker.workflow_id,
        "status": worker.status,
        "consecutive_health_failures": worker.consecutive_health_failures,
        "current_job_id": worker.current_job_id,
        "last_seen_at": worker.last_seen_at.isoformat() if worker.last_seen_at else None,
        "last_error": worker.last_error,
    }


def list_workers() -> list[dict]:
    db = SessionLocal()
    try:
        return [serialize_worker(item) for item in db.query(KaggleWorker).order_by(KaggleWorker.id).all()]
    finally:
        db.close()


def reserve_worker(project_id: int, scene_id: str) -> dict | None:
    """Atomically reserve one FREE worker; safe across concurrent projects/processes."""
    db = SessionLocal()
    try:
        candidate_id = (
            select(KaggleWorker.id)
            .where(KaggleWorker.status == "FREE")
            .order_by(KaggleWorker.id)
            .limit(1)
            .scalar_subquery()
        )
        reserved_id = db.execute(
            update(KaggleWorker)
            .where(
                KaggleWorker.id == candidate_id,
                KaggleWorker.status == "FREE",
            )
            .values(
                status="BUSY",
                current_project_id=project_id,
                current_scene_id=str(scene_id),
                current_job_id=None,
                updated_at=datetime.now(timezone.utc),
            )
            .returning(KaggleWorker.id)
        ).scalar_one_or_none()
        if reserved_id is None:
            db.rollback()
            return None
        db.commit()
        worker = db.get(KaggleWorker, reserved_id)
        return {
            **serialize_worker(worker),
            "api_key": decrypt_api_key(worker.encrypted_api_key),
        }
    finally:
        db.close()


def update_worker_job(worker_id: int, job_id: str) -> None:
    db = SessionLocal()
    try:
        db.execute(
            update(KaggleWorker).where(KaggleWorker.id == worker_id).values(
                current_job_id=str(job_id), updated_at=datetime.now(timezone.utc)
            )
        )
        db.commit()
    finally:
        db.close()


def release_worker(worker_id: int, offline: bool = False, error: str | None = None) -> None:
    db = SessionLocal()
    try:
        worker = db.get(KaggleWorker, worker_id)
        if worker:
            worker.status = "OFFLINE" if offline else "FREE"
            worker.current_job_id = None
            worker.current_project_id = None
            worker.current_scene_id = None
            worker.last_error = (error or "")[:1000] or None
            worker.updated_at = datetime.now(timezone.utc)
            if not offline:
                worker.consecutive_health_failures = 0
                worker.last_seen_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()


async def check_worker_health(worker: KaggleWorker) -> bool:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0)) as client:
            response = await client.get(f"{worker.api_url}/health")
            response.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("Kaggle worker %s health check failed: %s", worker.id, exc)
        return False


async def health_check_once() -> None:
    db = SessionLocal()
    try:
        workers = db.query(KaggleWorker).all()
        snapshot = [(w.id, w.api_url, w.status) for w in workers]
    finally:
        db.close()

    for worker_id, api_url, status in snapshot:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(12.0)) as client:
                response = await client.get(f"{api_url}/health")
                response.raise_for_status()
            db = SessionLocal()
            try:
                worker = db.get(KaggleWorker, worker_id)
                if worker:
                    worker.consecutive_health_failures = 0
                    worker.last_seen_at = datetime.now(timezone.utc)
                    if worker.status == "OFFLINE":
                        worker.status = "FREE"
                    worker.last_error = None
                    db.commit()
            finally:
                db.close()
        except Exception as exc:
            db = SessionLocal()
            try:
                worker = db.get(KaggleWorker, worker_id)
                if not worker:
                    continue
                worker.consecutive_health_failures += 1
                worker.last_error = str(exc)[:1000]
                if worker.consecutive_health_failures >= OFFLINE_FAILURE_LIMIT:
                    logger.error(
                        "Removing Kaggle worker %s after %s consecutive failed health checks.",
                        worker.id, worker.consecutive_health_failures,
                    )
                    db.delete(worker)
                else:
                    if worker.status != "BUSY":
                        worker.status = "OFFLINE"
                db.commit()
            finally:
                db.close()


async def health_monitor() -> None:
    while True:
        try:
            await health_check_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Kaggle worker health monitor failed")
        await asyncio.sleep(HEALTH_INTERVAL_SECONDS)



def worker_count() -> int:
    db = SessionLocal()
    try:
        return db.query(KaggleWorker).count()
    finally:
        db.close()
