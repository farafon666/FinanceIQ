"""Fresh committed probe tables and real service transactions for every test."""

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from financeiq.core.config import MigrationSettings, Settings
from financeiq.db.models import metadata
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


@pytest.fixture(scope="session")
def storage_postgres() -> Iterator[TestPostgres]:
    """Historical 2.2--2.6 storage contract, before complete-command guards.

    Those suites intentionally persist incomplete financial graphs. Head is
    exercised separately with complete commands, never by disabling triggers.
    """
    with TestPostgres() as database:
        result = database.alembic("upgrade", "0006")
        if result.returncode:
            raise RuntimeError("Historical storage migration failed.")
        yield database


@pytest_asyncio.fixture
async def financial_migrator(
    runtime_postgres: TestPostgres,
) -> AsyncIterator[AsyncEngine]:
    engine = create_migration_engine(
        MigrationSettings(
            migration_database_url=runtime_postgres.url(
                "financeiq_migrator", sqlalchemy=True
            )
        )
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def financial_engine(
    runtime_postgres: TestPostgres, financial_migrator: AsyncEngine
) -> AsyncIterator[AsyncEngine]:
    async def clear() -> None:
        # Only all tables in this fixture's disposable container; runtime cannot truncate.
        async with financial_migrator.begin() as connection:
            await connection.execute(text("TRUNCATE " + ",".join(metadata.tables)))

    engine = create_engine(
        Settings(
            app_env="test",
            database_url=runtime_postgres.url("financeiq_runtime", sqlalchemy=True),
        )
    )
    try:
        await clear()
        yield engine
    finally:
        await engine.dispose()
        await clear()


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
