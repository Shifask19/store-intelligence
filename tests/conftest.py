"""
conftest.py — Shared pytest fixtures for all API tests.

Sets DATABASE_URL to SQLite BEFORE any app module is imported,
so the engine is created pointing at the test DB.
"""

import asyncio
import os

# Must be set before any app.* import
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_shared.db"
os.environ["DB_NULL_POOL"] = "true"

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base
from app.database import get_db
from app.main import app

TEST_DB_URL = "sqlite+aiosqlite:///./test_shared.db"
test_engine = create_async_engine(TEST_DB_URL, echo=False)
TestSessionLocal = async_sessionmaker(
    bind=test_engine, class_=AsyncSession, expire_on_commit=False
)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


# Override the DB dependency for all tests
app.dependency_overrides[get_db] = override_get_db


# pytest-asyncio ≥0.21 deprecates the event_loop fixture override.
# Use asyncio_mode=auto (set in pytest.ini) and a session-scoped loop instead.
@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def create_tables():
    """Create all tables once per test session."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await test_engine.dispose()
    # Clean up the test DB file
    for fname in ("./test_shared.db", "./test_shared.db-shm", "./test_shared.db-wal"):
        try:
            os.remove(fname)
        except FileNotFoundError:
            pass


@pytest_asyncio.fixture(autouse=True)
async def clean_tables():
    """Wipe all rows before each test for isolation."""
    async with TestSessionLocal() as session:
        await session.execute(text("DELETE FROM events"))
        await session.execute(text("DELETE FROM pos_transactions"))
        await session.commit()
    yield
