"""Real Compose lifecycle in an isolated project; existing volumes are never used."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.compose

SECRET_NAMES = ("postgres_password", "migrator_password", "runtime_password")


class ComposeIntegrationTests(unittest.TestCase):
    project: ClassVar[str]
    temporary: ClassVar[tempfile.TemporaryDirectory[str]]
    directory: ClassVar[Path]
    passwords: ClassVar[dict[str, str]]
    sensitive_values: ClassVar[list[str]]
    environment: ClassVar[dict[str, str]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.project = f"financeiq-compose-test-{uuid4().hex}"
        secret_root = ROOT / ".secrets"
        secret_root.mkdir(exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(
            prefix="compose-test-", dir=secret_root
        )
        cls.directory = Path(cls.temporary.name).resolve()
        if not cls.directory.is_relative_to(secret_root.resolve()):
            raise RuntimeError("Test secrets must stay in the dedicated directory.")
        cls.addClassCleanup(cls.temporary.cleanup)
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "infra/compose/setup.py"),
                "--directory",
                str(cls.directory),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode:
            raise RuntimeError("Test secret preparation failed.")
        cls.passwords = {
            name: (cls.directory / name).read_text(encoding="utf-8")
            for name in SECRET_NAMES
        }
        cls.sensitive_values = [*cls.passwords.values(), "wrong-password-sentinel"]
        cls.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("FINANCEIQ_", "COMPOSE_"))
        }
        cls.environment.update(
            FINANCEIQ_SECRETS_DIR=cls.directory.as_posix(),
            FINANCEIQ_BACKEND_PORT="0",
            FINANCEIQ_POSTGRES_PORT="0",
        )
        cls.addClassCleanup(cls.cleanup_project)
        cls.compose("build", timeout=600)

    @classmethod
    def docker(
        cls, *arguments: str, check: bool = True, timeout: int = 120
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["docker", *arguments],
            cwd=ROOT,
            env=cls.environment,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if any(
            value in result.stdout + result.stderr for value in cls.sensitive_values
        ):
            raise AssertionError("Docker diagnostics exposed a test credential.")
        if check and result.returncode:
            raise AssertionError(
                "Docker command failed:\n" + result.stdout + result.stderr
            )
        return result

    @classmethod
    def compose(
        cls, *arguments: str, check: bool = True, timeout: int = 120
    ) -> subprocess.CompletedProcess[str]:
        return cls.docker(
            "compose",
            "--project-name",
            cls.project,
            "--env-file",
            str(ROOT / ".env.compose.example"),
            "--file",
            str(ROOT / "compose.yaml"),
            *arguments,
            check=check,
            timeout=timeout,
        )

    @classmethod
    def cleanup_project(cls) -> None:
        if not cls.project.startswith("financeiq-compose-test-"):
            raise RuntimeError("Refusing cleanup of an unrelated Compose project.")
        cls.compose("down", "--volumes", "--remove-orphans")
        cls.docker("image", "rm", f"{cls.project}-backend:local", check=False)

    def state(self, service: str) -> dict[str, Any] | None:
        identifier = self.compose("ps", "--all", "--quiet", service).stdout.strip()
        if not identifier:
            return None
        state: dict[str, Any] = json.loads(self.docker("inspect", identifier).stdout)[0]
        return state

    def required_state(self, service: str) -> dict[str, Any]:
        state = self.state(service)
        assert state is not None, f"Missing service: {service}"
        return state

    def wait_health(self, service: str, expected: str, timeout: int = 90) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.state(service)
            if state and state["State"].get("Health", {}).get("Status") == expected:
                return
            time.sleep(1)
        self.fail(f"{service} did not become {expected}.")

    def up(self) -> None:
        self.compose("up", "--wait", "--wait-timeout", "180", timeout=240)

    def health_url(self) -> str:
        address = self.compose("port", "backend", "8000").stdout.strip()
        self.assertTrue(address.startswith("127.0.0.1:"))
        return f"http://{address}/api/v1/health"

    def sql(self, statement: str) -> str:
        return self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-X",
            "-U",
            "postgres",
            "-d",
            "financeiq",
            "-v",
            "ON_ERROR_STOP=1",
            "-Atc",
            statement,
        ).stdout.strip()

    def replace_secret(self, name: str, value: str) -> None:
        path = self.directory / name
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        path.write_text(value, encoding="utf-8")
        path.chmod(0o444)

    def test_lifecycle_readiness_persistence_and_migration_failure(self) -> None:
        self.up()
        self.assertEqual(self.required_state("migrate")["State"]["ExitCode"], 0)
        self.assertEqual(
            self.required_state("backend")["State"]["Health"]["Status"], "healthy"
        )
        with httpx.Client(trust_env=False) as client:
            response = client.get(self.health_url(), timeout=5)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"status": "ok"})
        current = self.compose(
            "run", "--rm", "--no-deps", "migrate", "migrate", "current"
        )
        self.assertIn("0009 (head)", current.stdout)
        self.compose("run", "--rm", "--no-deps", "migrate", "migrate", "check")

        for service, target in (
            ("backend", "runtime_password"),
            ("migrate", "migrator_password"),
        ):
            state = self.required_state(service)
            self.assertEqual(state["Config"]["User"], "10001:10001")
            self.assertEqual(
                {mount["Destination"] for mount in state["Mounts"]},
                {f"/run/secrets/{target}"},
            )
            self.assertFalse(
                any("DATABASE_URL=" in entry for entry in state["Config"]["Env"])
            )
        self.compose(
            "exec",
            "-T",
            "backend",
            "python",
            "-c",
            "from pathlib import Path; import os; assert os.getuid() == 10001; "
            "assert Path('/run/secrets/runtime_password').read_text(); "
            "assert not Path('/run/secrets/migrator_password').exists(); "
            "assert not Path('/run/secrets/postgres_password').exists()",
        )
        self.sql(
            "CREATE TABLE public.compose_probe (value integer); INSERT INTO public.compose_probe VALUES (17)"
        )
        self.up()
        self.compose(
            "up", "--force-recreate", "--wait", "--wait-timeout", "180", timeout=240
        )
        self.assertEqual(self.sql("SELECT value FROM public.compose_probe"), "17")
        self.compose("down")
        self.up()
        self.assertEqual(self.sql("SELECT value FROM public.compose_probe"), "17")

        self.compose("stop", "postgres")
        self.wait_health("backend", "unhealthy")
        with httpx.Client(trust_env=False) as client:
            self.assertEqual(
                client.get(self.health_url(), timeout=5).json(), {"status": "ok"}
            )
        probe = self.compose(
            "exec",
            "-T",
            "backend",
            "python",
            "-m",
            "financeiq.core.container",
            "ready",
            check=False,
        )
        self.assertEqual(probe.returncode, 1)
        self.assertEqual(probe.stderr.strip(), "Container command failed.")
        self.compose("start", "postgres")
        self.wait_health("postgres", "healthy")
        self.wait_health("backend", "healthy")

        self.compose("down")
        self.replace_secret("migrator_password", "wrong-password-sentinel")
        failed = self.compose("up", "--wait", "--wait-timeout", "60", check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertNotEqual(self.required_state("migrate")["State"]["ExitCode"], 0)
        backend = self.state("backend")
        self.assertTrue(backend is None or not backend["State"]["Running"])
        self.compose("logs", "--no-color")
        # Repair the existing database from the current secret files, exactly
        # as documented; changing files or restarting alone does not rotate it.
        self.compose(
            "exec",
            "-T",
            "postgres",
            "bash",
            "/docker-entrypoint-initdb.d/10-financeiq.sh",
        )
        self.up()
        self.assertEqual(self.sql("SELECT value FROM public.compose_probe"), "17")
        self.compose("config")
        self.compose("logs", "--no-color")
        self.docker("history", "--no-trunc", f"{self.project}-backend:local")
        self.docker(
            "run",
            "--rm",
            "--entrypoint",
            "python",
            f"{self.project}-backend:local",
            "-c",
            "from pathlib import Path; "
            "assert not Path('/run/secrets/runtime_password').exists(); "
            "assert not Path('/app/.env').exists(); assert not Path('/app/.secrets').exists()",
        )


if __name__ == "__main__":
    unittest.main()
