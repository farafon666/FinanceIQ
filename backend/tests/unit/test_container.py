"""Container commands expose credentials only to the appropriate child process."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.engine import make_url

from financeiq.core import container
from financeiq.core.config import Settings


class ContainerCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.password = "secret-sentinel@:%/?#"
        for name in ("runtime_password", "migrator_password"):
            (self.directory / name).write_text(self.password, encoding="utf-8")
        self.secret_patch = patch.object(container, "SECRET_DIRECTORY", self.directory)
        self.secret_patch.start()
        self.addCleanup(self.secret_patch.stop)

    def test_serve_uses_only_runtime_credentials_and_executes_uvicorn(self):
        with (
            patch.dict(os.environ, FINANCEIQ_MIGRATION_DATABASE_URL="foreign-secret"),
            patch.object(container.os, "execve") as execute,
        ):
            self.assertEqual(container.main(["serve"]), 0)
        _, arguments, environment = execute.call_args.args
        self.assertIn("uvicorn", arguments)
        self.assertIn("--factory", arguments)
        self.assertNotIn("--reload", arguments)
        url = make_url(environment["FINANCEIQ_DATABASE_URL"])
        self.assertEqual(url.username, "financeiq_runtime")
        self.assertEqual(url.password, self.password)
        self.assertEqual(
            (url.host, url.port, url.database), ("postgres", 5432, "financeiq")
        )
        self.assertNotIn("FINANCEIQ_MIGRATION_DATABASE_URL", environment)
        self.assertNotIn(self.password, " ".join(arguments))

    def test_migrate_uses_only_migrator_and_preserves_alembic_arguments(self):
        with (
            patch.dict(os.environ, FINANCEIQ_DATABASE_URL="foreign-secret"),
            patch.object(container.os, "execve") as execute,
        ):
            self.assertEqual(container.main(["migrate", "current"]), 0)
        _, arguments, environment = execute.call_args.args
        self.assertEqual(arguments[-2:], ["alembic", "current"])
        url = make_url(environment["FINANCEIQ_MIGRATION_DATABASE_URL"])
        self.assertEqual(url.username, "financeiq_migrator")
        self.assertEqual(url.password, self.password)
        self.assertNotIn("FINANCEIQ_DATABASE_URL", environment)

    def test_missing_empty_invalid_or_unreadable_secret_fails_safely(self):
        secret = self.directory / "runtime_password"
        for content in (None, b"", b"secret-sentinel\nsecond-line", b"\xff"):
            with self.subTest(content=content):
                if secret.exists():
                    secret.unlink()
                if content is not None:
                    secret.write_bytes(content)
                output = io.StringIO()
                with (
                    redirect_stderr(output),
                    patch.object(container.os, "execve") as run,
                ):
                    self.assertEqual(container.main(["serve"]), 1)
                run.assert_not_called()
                self.assertEqual(output.getvalue(), "Container command failed.\n")
        with (
            patch.object(
                Path, "read_text", side_effect=PermissionError("secret-sentinel")
            ),
            redirect_stderr(io.StringIO()) as output,
        ):
            self.assertEqual(container.main(["serve"]), 1)
        self.assertEqual(output.getvalue(), "Container command failed.\n")

    def test_readiness_checks_http_and_database_without_exec(self):
        with (
            patch.object(container, "check_http") as http,
            patch.object(
                container, "check_database", new_callable=AsyncMock
            ) as database,
            patch.object(container.os, "execve") as execute,
        ):
            self.assertEqual(container.main(["ready"]), 0)
        http.assert_called_once_with()
        database.assert_awaited_once()
        settings = database.call_args.args[0]
        self.assertEqual(
            make_url(settings.database_url.get_secret_value()).username,
            "financeiq_runtime",
        )
        execute.assert_not_called()

    def test_readiness_failures_hide_original_errors(self):
        for boundary in ("check_http", "check_database"):
            with (
                self.subTest(boundary=boundary),
                patch.object(container, "check_http"),
                patch.object(container, "check_database", new_callable=AsyncMock),
                redirect_stderr(io.StringIO()) as output,
            ):
                with patch.object(
                    container,
                    boundary,
                    side_effect=RuntimeError("secret-sentinel SQL SELECT private"),
                ):
                    self.assertEqual(container.main(["ready"]), 1)
                self.assertEqual(output.getvalue(), "Container command failed.\n")

    def test_http_probe_validates_status_body_and_closes_connection(self):
        for status, body, accepted in (
            (200, b'{"status":"ok"}', True),
            (500, b'{"status":"ok"}', False),
            (200, b'{"status":"ok","extra":true}', False),
            (200, b"invalid", False),
        ):
            with self.subTest(status=status, body=body):
                connection = MagicMock()
                connection.getresponse.return_value.status = status
                connection.getresponse.return_value.read.return_value = body
                with patch.object(container, "HTTPConnection", return_value=connection):
                    if accepted:
                        container.check_http()
                    else:
                        with self.assertRaises((ValueError, RuntimeError)):
                            container.check_http()
                connection.close.assert_called_once_with()


class DatabaseReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_is_released_on_success_and_failure(self):
        settings = Settings(database_url="postgresql+asyncpg://r:unused@localhost/f")
        for fail in (False, True):
            with self.subTest(fail=fail):
                connection = AsyncMock()
                connection.scalar.return_value = 1
                if fail:
                    connection.scalar.side_effect = RuntimeError("secret-sentinel")
                engine = MagicMock()
                engine.connect.return_value.__aenter__ = AsyncMock(
                    return_value=connection
                )
                engine.connect.return_value.__aexit__ = AsyncMock(return_value=False)
                engine.dispose = AsyncMock()
                with patch.object(container, "create_engine", return_value=engine):
                    if fail:
                        with self.assertRaises(RuntimeError):
                            await container.check_database(settings)
                    else:
                        await container.check_database(settings)
                engine.dispose.assert_awaited_once()
                engine.connect.return_value.__aexit__.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
