"""Configuration behavior, runnable with unittest and later with pytest."""

import logging
import os
import subprocess
import sys
import tempfile
import traceback
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

from financeiq.core.config import ConfigurationError, load_settings


class ConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        clean_environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("FINANCEIQ_")
        }
        clean_environment["FINANCEIQ_DATABASE_URL"] = (
            "postgresql+asyncpg://test:secret-sentinel@127.0.0.1:5432/financeiq_test"
        )
        self.enterContext(patch.dict(os.environ, clean_environment, clear=True))
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.env_file = self.directory / ".env"

    def test_defaults(self) -> None:
        settings = load_settings()
        self.assertEqual(settings.app_env, "development")
        self.assertEqual(settings.log_level, "INFO")

    def test_database_url_is_required(self) -> None:
        del os.environ["FINANCEIQ_DATABASE_URL"]
        self.assert_safe_error(expected_field="FINANCEIQ_DATABASE_URL")

    def test_database_url_rejects_invalid_values(self) -> None:
        for value in (
            "",
            "secret-sentinel",
            "postgresql://test:secret-sentinel@localhost/db",
            "sqlite+aiosqlite:///secret-sentinel",
            "postgresql+asyncpg:///db",
            "postgresql+asyncpg://localhost/db",
            "postgresql+asyncpg://test@/db",
            "postgresql+asyncpg://test@localhost",
            "postgresql+asyncpg://test@localhost/",
            "postgresql+asyncpg://test@localhost:invalid/db",
            "postgresql+asyncpg://test@localhost:0/db",
            "postgresql+asyncpg://test@localhost:65536/db",
            " postgresql+asyncpg://test@localhost/db ",
        ):
            with self.subTest(value=value):
                os.environ["FINANCEIQ_DATABASE_URL"] = value
                self.assert_safe_error(expected_field="FINANCEIQ_DATABASE_URL")

    def test_database_url_accepts_encoded_password_and_ipv6(self) -> None:
        value = "postgresql+asyncpg://test:p%40ss%3Aword@[::1]:5432/financeiq_test"
        os.environ["FINANCEIQ_DATABASE_URL"] = value
        self.assertEqual(load_settings().database_url.get_secret_value(), value)

    def test_database_url_is_secret_in_settings_representations(self) -> None:
        settings = load_settings()
        for rendered in (
            str(settings),
            repr(settings),
            str(settings.model_dump()),
            settings.model_dump_json(),
        ):
            self.assertNotIn("secret-sentinel", rendered)

    def test_database_url_environment_overrides_explicit_file(self) -> None:
        self.env_file.write_text(
            "FINANCEIQ_DATABASE_URL=postgresql+asyncpg://file:other@localhost/file_db\n",
            encoding="utf-8",
        )
        self.assertEqual(
            load_settings(env_file=self.env_file).database_url.get_secret_value(),
            os.environ["FINANCEIQ_DATABASE_URL"],
        )
        del os.environ["FINANCEIQ_DATABASE_URL"]
        self.assertIn(
            "file_db",
            load_settings(env_file=self.env_file).database_url.get_secret_value(),
        )

    def test_all_allowed_environment_values(self) -> None:
        for value in ("development", "test", "production"):
            with self.subTest(app_env=value):
                os.environ["FINANCEIQ_APP_ENV"] = value
                self.assertEqual(load_settings().app_env, value)
        for value in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            with self.subTest(log_level=value):
                os.environ["FINANCEIQ_LOG_LEVEL"] = value
                self.assertEqual(load_settings().log_level, value)

    def test_explicit_utf8_file(self) -> None:
        self.env_file.write_text(
            "# Локальные настройки\n"
            "FINANCEIQ_APP_ENV=production\nFINANCEIQ_LOG_LEVEL=WARNING\n",
            encoding="utf-8",
        )
        settings = load_settings(env_file=self.env_file)
        self.assertEqual(settings.app_env, "production")
        self.assertEqual(settings.log_level, "WARNING")

    def test_process_environment_overrides_file(self) -> None:
        self.env_file.write_text(
            "FINANCEIQ_APP_ENV=production\nFINANCEIQ_LOG_LEVEL=ERROR\n",
            encoding="utf-8",
        )
        os.environ["FINANCEIQ_APP_ENV"] = "test"
        settings = load_settings(env_file=self.env_file)
        self.assertEqual(settings.app_env, "test")
        self.assertEqual(settings.log_level, "ERROR")

    def test_empty_explicit_file_uses_defaults(self) -> None:
        self.env_file.touch()
        self.assertEqual(load_settings(env_file=self.env_file), load_settings())

    def test_file_is_not_loaded_automatically(self) -> None:
        self.env_file.write_text("FINANCEIQ_APP_ENV=production\n", encoding="utf-8")
        with chdir(self.directory):
            self.assertEqual(load_settings().app_env, "development")

    def test_relative_path_is_resolved_from_working_directory(self) -> None:
        self.env_file.write_text("FINANCEIQ_APP_ENV=test\n", encoding="utf-8")
        with chdir(self.directory):
            self.assertEqual(load_settings(env_file=Path(".env")).app_env, "test")

    def test_relative_path_does_not_search_parent_directories(self) -> None:
        self.env_file.write_text("FINANCEIQ_APP_ENV=production\n", encoding="utf-8")
        child = self.directory / "child"
        child.mkdir()
        with chdir(child), self.assertRaises(ConfigurationError):
            load_settings(env_file=Path(".env"))

    def test_missing_file_is_an_error_even_with_process_values(self) -> None:
        os.environ["FINANCEIQ_APP_ENV"] = "production"
        with self.assertRaises(ConfigurationError):
            load_settings(env_file=self.env_file)

    def test_directory_is_not_an_environment_file(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_settings(env_file=self.directory)

    def test_unreadable_file_has_a_safe_error(self) -> None:
        self.env_file.touch()
        with patch("builtins.open", side_effect=PermissionError("secret-sentinel")):
            self.assert_safe_error(self.env_file)

    def test_invalid_utf8_file_has_a_safe_error(self) -> None:
        self.env_file.write_bytes(b"FINANCEIQ_APP_ENV=secret-sentinel\xff")
        self.assert_safe_error(self.env_file)

    def test_invalid_and_empty_values_are_rejected(self) -> None:
        for name, values in (
            ("FINANCEIQ_APP_ENV", ("", "Production", "staging", " production ")),
            ("FINANCEIQ_LOG_LEVEL", ("", "info", "TRACE", " INFO ")),
        ):
            for value in values:
                with self.subTest(name=name, value=value):
                    with patch.dict(os.environ, {name: value}):
                        with self.assertRaises(ConfigurationError) as caught:
                            load_settings()
                    self.assertIn(name, str(caught.exception))

    def test_invalid_file_value_has_a_safe_error(self) -> None:
        self.env_file.write_text(
            "FINANCEIQ_LOG_LEVEL=secret-sentinel\n", encoding="utf-8"
        )
        self.assert_safe_error(self.env_file, "FINANCEIQ_LOG_LEVEL")

    def test_invalid_process_value_has_a_safe_error(self) -> None:
        os.environ["FINANCEIQ_LOG_LEVEL"] = "secret-sentinel"
        self.assert_safe_error(expected_field="FINANCEIQ_LOG_LEVEL")

    def test_unknown_file_key_is_rejected_without_disclosing_key_or_value(self) -> None:
        for content in (
            "secret-sentinel-key=secret-sentinel-value\n",
            "secret-sentinel-key=\n",
            "secret-sentinel-key\n",
            "app_env=test\n",
        ):
            with self.subTest(content=content):
                self.env_file.write_text(content, encoding="utf-8")
                self.assert_safe_error(self.env_file)

    def test_file_key_without_a_value_is_rejected(self) -> None:
        self.env_file.write_text("FINANCEIQ_APP_ENV\n", encoding="utf-8")
        with self.assertRaises(ConfigurationError) as caught:
            load_settings(env_file=self.env_file)
        self.assertIn("FINANCEIQ_APP_ENV", str(caught.exception))

    def test_unrelated_process_variables_are_ignored(self) -> None:
        os.environ["UNRELATED_SETTING"] = "secret-sentinel"
        self.assertEqual(load_settings().app_env, "development")

    def test_environment_is_not_changed(self) -> None:
        self.env_file.write_text("FINANCEIQ_APP_ENV=production\n", encoding="utf-8")
        before = dict(os.environ)
        load_settings(env_file=self.env_file)
        self.assertEqual(dict(os.environ), before)

    def test_repeated_loads_are_independent(self) -> None:
        first = load_settings()
        os.environ["FINANCEIQ_APP_ENV"] = "test"
        second = load_settings()
        self.assertEqual(first.app_env, "development")
        self.assertEqual(second.app_env, "test")

    def test_settings_are_immutable(self) -> None:
        settings = load_settings()
        with self.assertRaises(ValueError):
            settings.app_env = "production"
        self.assertEqual(settings.app_env, "development")

    def test_loading_does_not_configure_logging(self) -> None:
        logger = logging.getLogger()
        before = (logger.level, list(logger.handlers))
        os.environ["FINANCEIQ_LOG_LEVEL"] = "DEBUG"
        load_settings()
        self.assertEqual((logger.level, list(logger.handlers)), before)

    def test_import_does_not_load_settings(self) -> None:
        os.environ["FINANCEIQ_APP_ENV"] = "secret-sentinel"
        result = subprocess.run(
            [sys.executable, "-c", "import financeiq.core.config"],
            cwd=self.directory,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_library_debugging_does_not_log_input_values(self) -> None:
        os.environ["PYDANTIC_SETTINGS_DEBUG"] = "1"
        os.environ["FINANCEIQ_APP_ENV"] = "secret-sentinel"
        with self.assertNoLogs("pydantic_settings", level="DEBUG"):
            self.assert_safe_error(expected_field="FINANCEIQ_APP_ENV")

    def assert_safe_error(
        self, env_file: Path | None = None, expected_field: str = ""
    ) -> None:
        try:
            load_settings(env_file=env_file)
        except ConfigurationError as error:
            rendered = "\n".join(
                (str(error), repr(error), "".join(traceback.format_exception(error)))
            )
            self.assertNotIn("secret-sentinel", rendered)
            self.assertNotIn("ValidationError", rendered)
            self.assertNotIn("During handling", rendered)
            self.assertIn(expected_field, str(error))
        else:
            self.fail("Expected a safe ConfigurationError")
