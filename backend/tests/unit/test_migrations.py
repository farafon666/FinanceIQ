"""CLI contract without a PostgreSQL server."""

import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy.pool import NullPool

from financeiq.core.config import MigrationSettings
from financeiq.db.session import create_migration_engine

BACKEND = Path(__file__).resolve().parents[2]


class MigrationCommandTests(unittest.TestCase):
    def run_alembic(
        self, *arguments: str, migration_url: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            k: v for k, v in os.environ.items() if not k.startswith("FINANCEIQ_")
        }
        environment["FINANCEIQ_DATABASE_URL"] = "secret-sentinel"
        if migration_url is not None:
            environment["FINANCEIQ_MIGRATION_DATABASE_URL"] = migration_url
        return subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=BACKEND,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_offline_generation_needs_no_credentials_or_connection(self) -> None:
        result = self.run_alembic("upgrade", "head", "--sql")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CREATE TABLE alembic_version", result.stdout)
        self.assertIn("0001", result.stdout)
        self.assertNotIn("secret-sentinel", result.stdout + result.stderr)

    def test_online_does_not_fall_back_to_runtime_credentials(self) -> None:
        result = self.run_alembic("--raiseerr", "upgrade", "head")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Migration operation failed.", result.stdout + result.stderr)
        for secret in ("secret-sentinel", "ValidationError", "During handling"):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_history_has_one_head_without_loading_credentials(self) -> None:
        result = self.run_alembic("heads")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0001 (head)")

    def test_connection_failure_is_safe(self) -> None:
        result = self.run_alembic(
            "--raiseerr",
            "upgrade",
            "head",
            migration_url="postgresql+asyncpg://m:secret-sentinel@127.0.0.1:1/financeiq",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Migration operation failed.", result.stderr)
        for secret in ("secret-sentinel", "asyncpg.exceptions", "During handling"):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_migration_engine_has_no_retained_pool(self) -> None:
        settings = MigrationSettings(
            migration_database_url="postgresql+asyncpg://m:unused@localhost/financeiq"
        )
        engine = create_migration_engine(settings)
        self.assertIsInstance(engine.pool, NullPool)
        self.assertFalse(engine.echo)
        self.assertTrue(engine.sync_engine.hide_parameters)

    def test_revision_template_produces_importable_typed_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(BACKEND / "migrations", root / "migrations")
            config = root / "alembic.ini"
            config.write_text(
                "[alembic]\nscript_location = %(here)s/migrations\n", encoding="utf-8"
            )
            result = self.run_alembic(
                "-c", str(config), "revision", "--rev-id", "0002", "-m", "probe"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            revision = next((root / "migrations" / "versions").glob("0002_*.py"))
            namespace = runpy.run_path(str(revision))
            self.assertEqual(namespace["revision"], "0002")
            self.assertEqual(namespace["down_revision"], "0001")
