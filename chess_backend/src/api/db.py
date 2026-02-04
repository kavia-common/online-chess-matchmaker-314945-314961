"""
Database utilities for the chess backend.

Uses SQLAlchemy AsyncEngine with asyncpg. Connection details are read from
environment variables (POSTGRES_URL, POSTGRES_USER, POSTGRES_PASSWORD, ...).
"""

from __future__ import annotations

import os
from typing import AsyncGenerator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def _build_async_db_url() -> str:
    """
    Build an asyncpg SQLAlchemy URL based on environment variables.

    Preference order:
    1) POSTGRES_URL (may be postgresql://... or already postgresql+asyncpg://...)
    2) Individual POSTGRES_* parts
    """
    raw_url = (os.getenv("POSTGRES_URL") or "").strip().strip('"').strip("'")
    if raw_url:
        if raw_url.startswith("postgresql+asyncpg://"):
            return raw_url
        if raw_url.startswith("postgresql://"):
            return raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        # Fallback: assume it's usable
        return raw_url

    user = (os.getenv("POSTGRES_USER") or "").strip().strip('"').strip("'")
    password = (os.getenv("POSTGRES_PASSWORD") or "").strip().strip('"').strip("'")
    db = (os.getenv("POSTGRES_DB") or "").strip().strip('"').strip("'")
    port = (os.getenv("POSTGRES_PORT") or "").strip().strip('"').strip("'") or "5432"
    host = "localhost"
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"


_ENGINE: Optional[AsyncEngine] = None
_SESSIONMAKER: Optional[async_sessionmaker[AsyncSession]] = None


# PUBLIC_INTERFACE
def get_engine() -> AsyncEngine:
    """Return a singleton AsyncEngine for the application."""
    global _ENGINE, _SESSIONMAKER
    if _ENGINE is None:
        db_url = _build_async_db_url()
        _ENGINE = create_async_engine(
            db_url,
            echo=False,
            future=True,
            pool_pre_ping=True,
        )
        _SESSIONMAKER = async_sessionmaker(_ENGINE, expire_on_commit=False)
    return _ENGINE


# PUBLIC_INTERFACE
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the AsyncSession maker bound to the singleton engine."""
    get_engine()
    assert _SESSIONMAKER is not None
    return _SESSIONMAKER


# PUBLIC_INTERFACE
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an AsyncSession."""
    session_maker = get_sessionmaker()
    async with session_maker() as session:
        yield session
