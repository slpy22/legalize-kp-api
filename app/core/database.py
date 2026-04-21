from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from qdrant_client import QdrantClient

from app.core.config import get_config

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_qdrant: QdrantClient | None = None


def get_db_url() -> str:
    """Build a postgresql+asyncpg connection URL from config."""
    cfg = get_config()["database"]
    return (
        f"postgresql+asyncpg://{cfg['user']}:{cfg['password']}"
        f"@{cfg['host']}:{cfg['port']}/{cfg['database']}"
    )


def get_engine() -> AsyncEngine:
    """Create and cache an async SQLAlchemy engine."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            get_db_url(),
            echo=False,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Create and cache an async session maker."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
        )
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Async generator yielding a session — intended for FastAPI Depends."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


def get_qdrant() -> QdrantClient:
    """Create and cache a Qdrant client."""
    global _qdrant
    if _qdrant is None:
        cfg = get_config()["qdrant"]
        _qdrant = QdrantClient(host=cfg["host"], port=cfg["port"])
    return _qdrant
