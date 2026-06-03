"""
app/database.py — SQLAlchemy async engine + session factory

Uses asyncpg for PostgreSQL in production and aiosqlite for tests.
The DATABASE_URL env var controls which backend is used.

Graceful degradation: if the DB is unreachable, callers catch
DatabaseUnavailableError and return HTTP 503.
"""

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import Base

# ---------------------------------------------------------------------------
# Engine setup
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://store_user:store_pass@localhost:5432/store_intelligence",
)

# NullPool is used in tests to avoid connection leaks across test cases.
# In production, the default pool is fine.
_use_null_pool = os.getenv("DB_NULL_POOL", "false").lower() == "true"

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,          # detect stale connections before use
    poolclass=NullPool if _use_null_pool else None,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,      # avoid lazy-load issues after commit
    autoflush=False,
)


# ---------------------------------------------------------------------------
# Dependency for FastAPI routes
# ---------------------------------------------------------------------------
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields a DB session and handles cleanup.
    Raises DatabaseUnavailableError if the connection fails.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# Schema initialisation (called on startup)
# ---------------------------------------------------------------------------
async def init_db() -> None:
    """Create all tables if they don't exist. Safe to call on every startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ---------------------------------------------------------------------------
# Health check helper
# ---------------------------------------------------------------------------
async def check_db_health() -> bool:
    """Returns True if the DB is reachable, False otherwise."""
    try:
        async with engine.connect() as conn:
            from sqlalchemy import text
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
