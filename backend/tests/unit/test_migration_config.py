"""Independent migration credentials and safe configuration diagnostics."""

import logging
import os
import tempfile
import traceback
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

from financeiq.core.config import (
    ConfigurationError,
    load_migration_settings,
    load_settings,
)


class MigrationConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        clean = {k: v for k, v in os.environ.items() if not k.startswith("FINANCEIQ_")}
        self.enterContext(patch.dict(os.environ, clean, clear=True))
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.env_file = self.directory / ".env.migrations"
        self.url = "postgresql+asyncpg://migrator:secret-sentinel@localhost/financeiq"

    def test_required_url_has_no_runtime_fallback(self) -> None:
        os.environ["FINANCEIQ_DATABASE_URL"] = self.url
        self.assert_safe_failure()

    def test_independent_secret_immutable_snapshot(self) -> None:
        os.environ["FINANCEIQ_MIGRATION_DATABASE_URL"] = self.url
        settings = load_migration_settings()
        self.assertEqual(settings.migration_database_url.get_secret_value(), self.url)
        for rendered in (str(settings), repr(settings), settings.model_dump_json()):
            self.assertNotIn("secret-sentinel", rendered)
        with self.assertRaises(ValueError):
            settings.migration_database_url = self.url
        os.environ["FINANCEIQ_MIGRATION_DATABASE_URL"] = self.url.replace(
            "localhost", "127.0.0.1"
        )
        self.assertEqual(settings.migration_database_url.get_secret_value(), self.url)

    def test_runtime_does_not_load_migration_credentials(self) -> None:
        os.environ["FINANCEIQ_DATABASE_URL"] = self.url
        os.environ["FINANCEIQ_MIGRATION_DATABASE_URL"] = "secret-sentinel"
        self.assertNotIn("migration_database_url", load_settings().model_dump())

    def test_explicit_file_and_environment_priority(self) -> None:
        self.env_file.write_text(
            f"# Миграции\nFINANCEIQ_MIGRATION_DATABASE_URL={self.url}\n",
            encoding="utf-8",
        )
        self.assertEqual(
            load_migration_settings(
                env_file=self.env_file
            ).migration_database_url.get_secret_value(),
            self.url,
        )
        override = self.url.replace("localhost", "127.0.0.1")
        os.environ["FINANCEIQ_MIGRATION_DATABASE_URL"] = override
        self.assertEqual(
            load_migration_settings(
                env_file=self.env_file
            ).migration_database_url.get_secret_value(),
            override,
        )

    def test_no_implicit_file(self) -> None:
        (self.directory / ".env").write_text(
            f"FINANCEIQ_MIGRATION_DATABASE_URL={self.url}\n", encoding="utf-8"
        )
        with chdir(self.directory):
            self.assert_safe_failure()

    def test_invalid_values_and_files_have_safe_errors(self) -> None:
        for value in (
            "",
            "secret-sentinel",
            "sqlite:///secret-sentinel",
            self.url + " ",
        ):
            with self.subTest(value=value):
                os.environ["FINANCEIQ_MIGRATION_DATABASE_URL"] = value
                self.assert_safe_failure()
        os.environ["FINANCEIQ_MIGRATION_DATABASE_URL"] = self.url
        for content in (
            "secret-sentinel=\n",
            "secret-sentinel\n",
            "FINANCEIQ_MIGRATION_DATABASE_URL\n",
            "FINANCEIQ_DATABASE_URL=secret-sentinel\n",
        ):
            self.env_file.write_text(content, encoding="utf-8")
            with self.subTest(content=content):
                self.assert_safe_failure(self.env_file)
        self.env_file.write_bytes(b"\xff")
        self.assert_safe_failure(self.env_file)
        self.assert_safe_failure(self.directory / "missing")

    def test_load_does_not_change_environment_or_logging(self) -> None:
        os.environ["FINANCEIQ_MIGRATION_DATABASE_URL"] = self.url
        os.environ["PYDANTIC_SETTINGS_DEBUG"] = "1"
        before = dict(os.environ)
        root = logging.getLogger()
        logging_before = root.level, list(root.handlers)
        with self.assertNoLogs("pydantic_settings", level="DEBUG"):
            load_migration_settings()
        self.assertEqual(dict(os.environ), before)
        self.assertEqual((root.level, list(root.handlers)), logging_before)

    def assert_safe_failure(self, env_file: Path | None = None) -> None:
        with self.assertRaises(ConfigurationError) as caught:
            load_migration_settings(env_file=env_file)
        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn("secret-sentinel", rendered)
        self.assertNotIn("ValidationError", rendered)
        self.assertNotIn("During handling", rendered)
