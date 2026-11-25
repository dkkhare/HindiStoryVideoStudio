from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

class Base(DeclarativeBase):
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db() -> None:
    from app import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    # Lightweight forward migration for existing SQLite installations.
    if settings.database_url.startswith("sqlite"):
        from sqlalchemy import inspect, text
        columns = {item["name"] for item in inspect(engine).get_columns("projects")}
        if "provider_config_json" not in columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE projects ADD COLUMN provider_config_json TEXT"))
