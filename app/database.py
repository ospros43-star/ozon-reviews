import os
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

# На Railway создаём директорию для БД если нужно
db_url = settings.database_url
if db_url.startswith("sqlite:///"):
    db_path = db_url.replace("sqlite:///", "").replace("//", "/")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    db_url,
    connect_args={"check_same_thread": False, "timeout": 30},
    pool_timeout=30,
)
# WAL mode — позволяет одновременные чтение и запись
with engine.connect() as conn:
    conn.execute(__import__("sqlalchemy").text("PRAGMA journal_mode=WAL"))
    conn.commit()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app.models import review  # noqa: F401
    from app.models import supply_plan  # noqa: F401
    Base.metadata.create_all(bind=engine)
