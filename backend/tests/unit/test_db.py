"""Database lifecycle tests without a PostgreSQL server or persistence claims."""

import asyncio
import logging
import os
import traceback
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncSessionTransaction,
    async_sessionmaker,
)

from financeiq.core.config import load_settings
from financeiq.db.errors import DatabaseError, finish_cleanup
from financeiq.db.session import (
    create_engine,
    create_session_factory,
    session_scope,
    transaction,
)
from financeiq.main import create_app

SENTINEL = "secret-sentinel SELECT private_value WHERE amount=123.45"


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_cleanup_failure_does_not_replace_cancellation(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FINANCEIQ_DATABASE_URL": "postgresql+asyncpg://test:unused@127.0.0.1/financeiq_test"
            },
        ):
            app = create_app()
        with (
            patch(
                "sqlalchemy.ext.asyncio.AsyncEngine.dispose",
                new=AsyncMock(side_effect=OSError(SENTINEL)),
            ) as dispose,
            self.assertRaises(asyncio.CancelledError),
        ):
            async with app.router.lifespan_context(app):
                raise asyncio.CancelledError()
        dispose.assert_awaited_once()

    async def test_service_can_complete_sequential_transactions(self) -> None:
        async with session_scope(async_sessionmaker(autobegin=False)) as session:
            for _ in range(2):
                async with transaction(session):
                    self.assertTrue(session.in_transaction())
                self.assertFalse(session.in_transaction())

    async def test_business_failure_rolls_back_and_remains_a_business_failure(
        self,
    ) -> None:
        session = AsyncSession(autobegin=False)
        with self.assertRaisesRegex(ValueError, "business failure"):
            async with session_scope(lambda: session):
                async with transaction(session):
                    raise ValueError("business failure")
        self.assertFalse(session.in_transaction())

    async def test_flush_failure_is_safe_and_rolls_back(self) -> None:
        session = AsyncSession(autobegin=False)
        with (
            patch.object(
                session,
                "flush",
                new=AsyncMock(
                    side_effect=IntegrityError(
                        SENTINEL, {"private": SENTINEL}, Exception(SENTINEL)
                    )
                ),
            ),
            self.assertRaises(DatabaseError) as caught,
        ):
            async with session_scope(lambda: session):
                async with transaction(session):
                    await session.flush()
        self.assertFalse(session.in_transaction())
        self.assert_safe(caught.exception)

    async def test_commit_failure_never_returns_success_or_retries(self) -> None:
        session = AsyncSession(autobegin=False)
        successful = False
        with (
            patch.object(
                AsyncSessionTransaction,
                "commit",
                new=AsyncMock(
                    side_effect=OperationalError(SENTINEL, {}, Exception(SENTINEL))
                ),
            ) as commit,
            self.assertRaises(DatabaseError) as caught,
        ):
            async with session_scope(lambda: session):
                async with transaction(session):
                    pass
                successful = True
        self.assertFalse(successful)
        self.assertFalse(session.in_transaction())
        commit.assert_awaited_once()
        self.assert_safe(caught.exception)

    async def test_scope_closes_unfinished_transaction_without_commit(self) -> None:
        session = AsyncSession(autobegin=False)
        with patch.object(session, "commit", new=AsyncMock()) as commit:
            async with session_scope(lambda: session):
                await session.begin()
                self.assertTrue(session.in_transaction())
            self.assertFalse(session.in_transaction())
            commit.assert_not_awaited()

    async def test_concurrent_scopes_have_independent_sessions(self) -> None:
        factory = async_sessionmaker(autobegin=False)
        ready = asyncio.Event()
        seen = []

        async def phase() -> None:
            async with session_scope(factory) as session:
                seen.append(session)
                async with transaction(session):
                    if len(seen) == 2:
                        ready.set()
                    await ready.wait()
                    self.assertTrue(session.in_transaction())

        await asyncio.wait_for(asyncio.gather(phase(), phase()), timeout=5)
        self.assertIsNot(seen[0], seen[1])
        self.assertFalse(any(session.in_transaction() for session in seen))

    async def test_cancellation_rolls_back_closes_and_propagates(self) -> None:
        started = asyncio.Event()
        session = AsyncSession(autobegin=False)
        with patch.object(
            session, "close", new=AsyncMock(wraps=session.close)
        ) as close:

            async def command() -> None:
                async with session_scope(lambda: session):
                    async with transaction(session):
                        started.set()
                        await asyncio.Future()

            task = asyncio.create_task(command())
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(session.in_transaction())
            close.assert_awaited_once()

    async def test_sql_execution_cancellation_survives_error_hook_and_cleanup(
        self,
    ) -> None:
        with patch.dict(
            os.environ,
            {
                "FINANCEIQ_DATABASE_URL": "postgresql+asyncpg://test:unused@127.0.0.1/financeiq_test"
            },
        ):
            engine = create_engine(load_settings())
        cancellation = asyncio.CancelledError()
        connection = MagicMock()
        connection.is_valid = True
        connection.cursor.return_value.execute.side_effect = cancellation
        session = create_session_factory(engine)()
        try:
            with (
                patch.object(
                    engine.sync_engine, "raw_connection", return_value=connection
                ),
                patch.object(
                    session, "rollback", new=AsyncMock(wraps=session.rollback)
                ) as rollback,
                patch.object(
                    session, "close", new=AsyncMock(wraps=session.close)
                ) as close,
                patch.object(
                    AsyncSessionTransaction, "commit", new=AsyncMock()
                ) as commit,
                self.assertRaises(asyncio.CancelledError) as caught,
            ):
                async with session_scope(lambda: session):
                    async with transaction(session):
                        await session.execute(
                            text("SELECT :private_value"), {"private_value": SENTINEL}
                        )
            self.assertIs(caught.exception, cancellation)
            connection.cursor.return_value.execute.assert_called_once()
            connection.invalidate.assert_called_once_with(cancellation)
            rollback.assert_awaited_once()
            close.assert_awaited_once()
            commit.assert_not_awaited()
            self.assertFalse(session.in_transaction())
        finally:
            await session.close()
            await engine.dispose()

    async def test_cleanup_failures_do_not_replace_cancellation(self) -> None:
        session = AsyncSession(autobegin=False)
        with (
            patch.object(
                session,
                "rollback",
                new=AsyncMock(
                    side_effect=OperationalError(SENTINEL, {}, Exception(SENTINEL))
                ),
            ),
            patch.object(
                session, "close", new=AsyncMock(side_effect=OSError(SENTINEL))
            ),
            self.assertRaises(asyncio.CancelledError),
        ):
            async with session_scope(lambda: session):
                async with transaction(session):
                    raise asyncio.CancelledError()
        await session.close()

    async def test_rollback_and_close_failures_are_safe(self) -> None:
        for method in ("rollback", "close"):
            with self.subTest(method=method):
                session = AsyncSession(autobegin=False)
                with (
                    patch.object(
                        session, method, new=AsyncMock(side_effect=OSError(SENTINEL))
                    ),
                    self.assertRaises(DatabaseError) as caught,
                ):
                    async with session_scope(lambda session=session: session):
                        async with transaction(session):
                            raise ValueError("business failure")
                self.assert_safe(caught.exception)
                await session.close()

    async def test_repeated_cancellation_waits_for_cleanup(self) -> None:
        started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def cleanup() -> None:
            started.set()
            await release.wait()
            completed.set()
            raise OSError(SENTINEL)

        task = asyncio.create_task(finish_cleanup(cleanup()))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(completed.is_set())

    async def test_engine_is_lazy_and_suppresses_raw_engine_and_pool_logs(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FINANCEIQ_DATABASE_URL": "postgresql+asyncpg://test:unused@127.0.0.1/financeiq_test"
            },
        ):
            settings = load_settings()
        root = logging.getLogger()
        before = (root.level, list(root.handlers))
        with patch("asyncpg.connect", new=AsyncMock()) as connect:
            engine = create_engine(settings)
            factory = create_session_factory(engine)
            async with session_scope(factory) as session:
                self.assertFalse(session.in_transaction())
                self.assertFalse(session.autoflush)
                self.assertFalse(session.sync_session.expire_on_commit)
                self.assertFalse(session.sync_session.autobegin)
            with self.assertNoLogs("sqlalchemy", level="DEBUG"):
                engine.sync_engine.logger.debug(SENTINEL)
                engine.sync_engine.pool.logger.error(
                    SENTINEL, exc_info=OSError(SENTINEL)
                )
            await engine.dispose()
            connect.assert_not_awaited()
        self.assertEqual((root.level, list(root.handlers)), before)

    def assert_safe(self, error: BaseException) -> None:
        rendered = "".join(traceback.format_exception(error))
        self.assertNotIn("secret-sentinel", rendered)
        self.assertNotIn("SELECT private_value", rendered)
        self.assertNotIn("OperationalError", rendered)
        self.assertEqual(str(error), "Database operation failed.")
