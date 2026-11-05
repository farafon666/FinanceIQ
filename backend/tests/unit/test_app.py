"""HTTP application behavior, runnable with unittest and later with pytest."""

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

from fastapi.testclient import TestClient

from financeiq.core.config import ConfigurationError
from financeiq.main import create_app


class ApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        clean_environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("FINANCEIQ_")
        }
        clean_environment["FINANCEIQ_DATABASE_URL"] = (
            "postgresql+asyncpg://test:unused@127.0.0.1:5432/financeiq_test"
        )
        self.enterContext(patch.dict(os.environ, clean_environment, clear=True))
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.env_file = self.directory / ".env"

    def test_health_is_public_and_exact_in_every_environment(self) -> None:
        for app_env in ("development", "test", "production"):
            with self.subTest(app_env=app_env):
                os.environ["FINANCEIQ_APP_ENV"] = app_env
                app = create_app()
                self.assertFalse(app.debug)
                with TestClient(app) as client:
                    response = client.get("/api/v1/health")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "application/json")
                self.assertEqual(response.content, b'{"status":"ok"}')

    def test_health_requires_versioned_path_and_get_method(self) -> None:
        with TestClient(create_app()) as client:
            self.assertEqual(client.get("/health").status_code, 404)
            self.assertEqual(client.post("/api/v1/health").status_code, 405)

    def test_development_and_test_expose_documented_health(self) -> None:
        for app_env in ("development", "test"):
            with self.subTest(app_env=app_env):
                os.environ["FINANCEIQ_APP_ENV"] = app_env
                with TestClient(create_app()) as client:
                    docs = client.get("/api/v1/docs")
                    response = client.get("/api/v1/openapi.json")
                self.assertEqual(docs.status_code, 200)
                self.assertIn("/api/v1/openapi.json", docs.text)
                self.assertEqual(response.status_code, 200)
                schema = response.json()
                self.assertEqual(set(schema["paths"]), {"/api/v1/health"})
                operation = schema["paths"]["/api/v1/health"]["get"]
                self.assertNotIn("security", operation)
                result = operation["responses"]["200"]["content"]["application/json"]
                model_name = result["schema"]["$ref"].rsplit("/", 1)[1]
                model = schema["components"]["schemas"][model_name]
                self.assertEqual(set(model["properties"]), {"status"})
                self.assertEqual(model["properties"]["status"]["const"], "ok")
                self.assertIn("status", model["required"])

    def test_production_does_not_expose_documentation(self) -> None:
        os.environ["FINANCEIQ_APP_ENV"] = "production"
        with TestClient(create_app()) as client:
            for path in ("/api/v1/docs", "/api/v1/openapi.json"):
                with self.subTest(path=path):
                    self.assertEqual(client.get(path).status_code, 404)

    def test_unused_documentation_routes_are_absent_in_every_environment(self) -> None:
        for app_env in ("development", "test", "production"):
            os.environ["FINANCEIQ_APP_ENV"] = app_env
            with TestClient(create_app()) as client:
                for path in (
                    "/docs",
                    "/redoc",
                    "/openapi.json",
                    "/docs/oauth2-redirect",
                    "/api/v1/redoc",
                    "/api/v1/docs/oauth2-redirect",
                ):
                    with self.subTest(app_env=app_env, path=path):
                        self.assertEqual(client.get(path).status_code, 404)

    def test_explicit_file_and_process_environment_configure_application(self) -> None:
        self.env_file.write_text(
            "FINANCEIQ_APP_ENV=production\nFINANCEIQ_LOG_LEVEL=WARNING\n",
            encoding="utf-8",
        )
        from_file = create_app(env_file=self.env_file)
        self.assertEqual(from_file.state.settings.log_level, "WARNING")
        with TestClient(from_file) as client:
            self.assertEqual(client.get("/api/v1/docs").status_code, 404)

        os.environ["FINANCEIQ_APP_ENV"] = "test"
        overridden = create_app(env_file=self.env_file)
        self.assertEqual(overridden.state.settings.app_env, "test")
        with TestClient(overridden) as client:
            self.assertEqual(client.get("/api/v1/docs").status_code, 200)

    def test_factory_does_not_discover_environment_files(self) -> None:
        self.env_file.write_text("FINANCEIQ_APP_ENV=production\n", encoding="utf-8")
        with chdir(self.directory), TestClient(create_app()) as client:
            self.assertEqual(client.get("/api/v1/docs").status_code, 200)

    def test_invalid_configuration_fails_without_exposing_values(self) -> None:
        os.environ["FINANCEIQ_APP_ENV"] = "secret-sentinel"
        with self.assertRaises(ConfigurationError) as caught:
            create_app()
        self.assertNotIn(
            "secret-sentinel", "".join(traceback.format_exception(caught.exception))
        )
        self.assertIn("FINANCEIQ_APP_ENV", str(caught.exception))

    def test_missing_explicit_file_prevents_application_creation(self) -> None:
        with self.assertRaises(ConfigurationError):
            create_app(env_file=self.env_file)

    def test_applications_keep_independent_configuration_snapshots(self) -> None:
        first = create_app()
        os.environ["FINANCEIQ_APP_ENV"] = "production"
        second = create_app()
        self.assertIsNot(first, second)
        self.assertIsNot(first.state.settings, second.state.settings)
        self.assertEqual(first.state.settings.app_env, "development")
        self.assertEqual(second.state.settings.app_env, "production")
        with TestClient(first) as client:
            self.assertEqual(client.get("/api/v1/docs").status_code, 200)
        with TestClient(second) as client:
            self.assertEqual(client.get("/api/v1/docs").status_code, 404)

    def test_factory_preserves_environment_and_logging(self) -> None:
        self.env_file.write_text("FINANCEIQ_LOG_LEVEL=DEBUG\n", encoding="utf-8")
        logger = logging.getLogger()
        logging_before = (logger.level, list(logger.handlers))
        environment_before = dict(os.environ)
        create_app(env_file=self.env_file)
        self.assertEqual(dict(os.environ), environment_before)
        self.assertEqual((logger.level, list(logger.handlers)), logging_before)

    def test_import_does_not_load_settings_create_app_or_start_server(self) -> None:
        os.environ["FINANCEIQ_APP_ENV"] = "secret-sentinel"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from unittest.mock import patch\n"
                "with (\n"
                "    patch('financeiq.core.config.load_settings', "
                "side_effect=AssertionError('Settings loaded during import')),\n"
                "    patch('fastapi.FastAPI', "
                "side_effect=AssertionError('Application created during import')),\n"
                "    patch('uvicorn.run', "
                "side_effect=AssertionError('Server started during import')),\n"
                "    patch('sqlalchemy.ext.asyncio.create_async_engine', "
                "side_effect=AssertionError('Engine created during import')),\n"
                "):\n"
                "    import financeiq.main\n",
            ],
            cwd=self.directory,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
