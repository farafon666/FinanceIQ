"""Fresh committed probe tables and real service transactions for every test."""

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financeiq.core.config import MigrationSettings, Settings
from financeiq.db.session import (
    create_engine,
    create_migration_engine,
    create_session_factory,
)
from tests.support.postgres import TestPostgres
from tests.support.probes import ProbeBase


@pytest.fixture(scope="session")
def runtime_postgres() -> Iterator[TestPostgres]:
    with TestPostgres() as database:
        result = database.alembic("upgrade", "head")
        if result.returncode:
            raise RuntimeError("Test runtime migration failed.")
        yield database


@pytest_asyncio.fixture
async def probe_tables(runtime_postgres: TestPostgres) -> AsyncIterator[None]:
    engine = create_migration_engine(
        MigrationSettings(
            migration_database_url=runtime_postgres.url(
                "financeiq_migrator", sqlalchemy=True
            )
        )
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(ProbeBase.metadata.create_all)
            await connection.execute(
                text(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON "
                    "test_runtime_parent, test_runtime_entry TO financeiq_runtime"
                )
            )
        yield
    finally:
        try:
            async with engine.begin() as connection:
                await connection.run_sync(ProbeBase.metadata.drop_all)
        finally:
            await engine.dispose()


@pytest_asyncio.fixture
async def runtime_sessions(
    runtime_postgres: TestPostgres,
    probe_tables: None,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(
        Settings(
            app_env="test",
            database_url=runtime_postgres.url("financeiq_runtime", sqlalchemy=True),
        )
    )
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()
