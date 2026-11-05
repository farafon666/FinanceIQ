"""Exercise DB errors through HTTP using synthetic failures, never PostgreSQL."""

import os
import traceback
import unittest
from unittest.mock import AsyncMock, patch

from asyncpg import InterfaceError, InvalidPasswordError
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    AsyncSessionTransaction,
    async_sessionmaker,
)

from financeiq.api.dependencies import DatabaseSession
from financeiq.db.errors import DatabaseError
from financeiq.db.session import transaction
from financeiq.main import create_app

SENTINEL = "secret-sentinel SELECT private_value WHERE amount=123.45"


class DatabaseApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("FINANCEIQ_")
        }
        environment["FINANCEIQ_DATABASE_URL"] = (
            "postgresql+asyncpg://test:secret-sentinel@127.0.0.1:5432/financeiq_test"
        )
        self.enterContext(patch.dict(os.environ, environment, clear=True))

    def test_import_factory_lifespan_and_health_never_connect(self) -> None:
        with patch("asyncpg.connect", new=AsyncMock()) as connect:
            first, second = create_app(), create_app()
            self.assertFalse(hasattr(first.state, "engine"))
            with TestClient(first) as client, TestClient(second):
                self.assertIsNot(first.state.engine, second.state.engine)
                self.assertIsNot(
                    first.state.session_factory, second.state.session_factory
                )
                response = client.get("/api/v1/health")
                self.assertEqual(response.content, b'{"status":"ok"}')
            connect.assert_not_awaited()

    def test_sessions_are_cached_within_request_and_separate_between_requests(
        self,
    ) -> None:
        app = create_app()
        seen = []

        @app.get("/test-session")
        async def probe(
            session: DatabaseSession, cached: DatabaseSession
        ) -> dict[str, bool]:
            seen.append(session)
            return {"cached": session is cached}

        with TestClient(app) as client:
            self.assertEqual(client.get("/test-session").json(), {"cached": True})
            self.assertEqual(client.get("/test-session").json(), {"cached": True})
        self.assertIsNot(seen[0], seen[1])

    def test_connection_failure_is_safe_with_debug_logging(self) -> None:
        app = create_app()

        @app.get("/test-query")
        async def query(session: DatabaseSession) -> dict[str, bool]:
            async with transaction(session):
                await session.execute(
                    text("SELECT :private_value"), {"private_value": SENTINEL}
                )
            return {"success": True}

        for failure in (
            ConnectionRefusedError(SENTINEL),
            TimeoutError(SENTINEL),
            InvalidPasswordError(SENTINEL),
            InterfaceError(SENTINEL),
            ValueError(SENTINEL),
            TypeError(SENTINEL),
        ):
            with (
                self.subTest(failure=type(failure).__name__),
                patch("asyncpg.connect", new=AsyncMock(side_effect=failure)) as connect,
                self.assertLogs(level="DEBUG") as logs,
                TestClient(app) as client,
            ):
                response = client.get("/test-query")
            self.assert_safe_response(response, logs.output)
            connect.assert_awaited_once()
            self.assertEqual(connect.call_args.kwargs["timeout"], 5)

    def test_commit_failure_cannot_send_success(self) -> None:
        app = create_app()

        @app.get("/test-commit")
        async def command(session: DatabaseSession) -> dict[str, bool]:
            async with transaction(session):
                pass
            return {"success": True}

        with (
            patch.object(
                AsyncSessionTransaction,
                "commit",
                new=AsyncMock(side_effect=OSError(SENTINEL)),
            ) as commit,
            self.assertLogs(level="DEBUG") as logs,
            TestClient(app) as client,
        ):
            app.state.session_factory = async_sessionmaker(autobegin=False)
            response = client.get("/test-commit")
        self.assert_safe_response(response, logs.output)
        commit.assert_awaited_once()

    def test_session_close_failure_happens_before_response_is_sent(self) -> None:
        app = create_app()

        @app.get("/test-close")
        async def command(session: DatabaseSession) -> dict[str, bool]:
            return {"success": True}

        with (
            patch.object(
                AsyncSession, "close", new=AsyncMock(side_effect=OSError(SENTINEL))
            ),
            self.assertLogs(level="DEBUG") as logs,
            TestClient(app) as client,
        ):
            response = client.get("/test-close")
        self.assert_safe_response(response, logs.output)

    def test_lifespan_disposes_engine_and_sanitizes_disposal_failure(self) -> None:
        app = create_app()
        with (
            patch.object(
                AsyncEngine, "dispose", new=AsyncMock(side_effect=OSError(SENTINEL))
            ) as dispose,
            self.assertRaises(DatabaseError) as caught,
        ):
            with TestClient(app):
                pass
        dispose.assert_awaited_once()
        self.assertNotIn(
            "secret-sentinel", "".join(traceback.format_exception(caught.exception))
        )

    def assert_safe_response(self, response, logs) -> None:
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Database operation failed."})
        rendered = response.text + "\n".join(logs)
        for private in ("secret-sentinel", "private_value", "123.45", "Traceback"):
            self.assertNotIn(private, rendered)
