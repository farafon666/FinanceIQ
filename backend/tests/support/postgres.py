"""Disposable PostgreSQL shared infrastructure, without shared database state."""

import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Literal, Self
from urllib.parse import quote
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[2]
BOOTSTRAP = BACKEND.parent / "infra/postgres/bootstrap.sql"
POSTGRES_IMAGE = (
    "postgres:18.7@sha256:"
    "2ea8630df176ad090ec92136dee850397c5af8781e4bcf2e4162593691c61bbc"
)
Role = Literal["postgres", "financeiq_migrator", "financeiq_runtime"]

REVISION_0006_TABLES = frozenset(
    {
        "accounts",
        "auth_sessions",
        "balance_confirmations",
        "categories",
        "categorization_rules",
        "financial_action_transactions",
        "financial_actions",
        "history_confirmations",
        "import_account_mappings",
        "import_attempts",
        "import_batches",
        "import_cancellations",
        "import_movements",
        "import_rows",
        "transactions",
        "transfer_links",
        "users",
    }
)


@dataclass
class TestPostgres:
    """Own one container; never accept a caller's database URL or resource name."""

    __test__ = False
    container: str = field(
        default_factory=lambda: f"financeiq-pg-test-{uuid4().hex}", init=False
    )
    port: int = field(default=0, init=False)
    passwords: dict[Role, str] = field(default_factory=dict, init=False, repr=False)

    def __enter__(self) -> Self:
        self.passwords = {
            role: secrets.token_hex(24)
            for role in ("postgres", "financeiq_migrator", "financeiq_runtime")
        }
        self.passwords["financeiq_migrator"] += "@:%"
        environment = dict(os.environ, POSTGRES_PASSWORD=self.passwords["postgres"])
        try:
            self.docker(
                "run",
                "--detach",
                "--rm",
                "--name",
                self.container,
                "--label",
                "financeiq.test=postgres",
                "--tmpfs",
                "/var/lib/postgresql",
                "--publish",
                "127.0.0.1::5432",
                "--env",
                "POSTGRES_PASSWORD",
                "--env",
                "POSTGRES_DB=financeiq",
                POSTGRES_IMAGE,
                environment=environment,
            )
            deadline = time.monotonic() + 30
            while self.docker(
                "exec",
                self.container,
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-U",
                "postgres",
                "-d",
                "financeiq",
                check=False,
            ).returncode:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Test PostgreSQL did not become ready.")
                time.sleep(0.2)
            address = self.docker("port", self.container, "5432/tcp").stdout.strip()
            if not address.startswith("127.0.0.1:"):
                raise RuntimeError("Test PostgreSQL must bind only to loopback.")
            self.port = int(address.rsplit(":", 1)[1])
            self.bootstrap(check=True)
            # Credentials travel through stdin, never Docker/psql arguments.
            self.psql(
                "\n".join(
                    f"ALTER ROLE {role} PASSWORD '{password}';"
                    for role, password in self.passwords.items()
                    if role != "postgres"
                )
            )
            return self
        except BaseException:
            self.stop()
            raise

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    def stop(self) -> None:
        # The name is generated here, never read from environment/configuration.
        if not self.container.startswith("financeiq-pg-test-"):
            raise RuntimeError("Refusing cleanup of an unrelated container.")
        result = self.docker("rm", "--force", self.container, check=False)
        if result.returncode and "No such container" not in result.stderr:
            raise RuntimeError("Test PostgreSQL cleanup failed.")

    def assert_safe(self, output: str) -> None:
        for secret in (
            *self.passwords.values(),
            "secret-sentinel",
            "wrong-password-sentinel",
        ):
            if secret in output or quote(secret, safe="") in output:
                raise AssertionError("Test diagnostics exposed a credential.")

    def docker(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
        input: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["docker", *arguments],
                env=environment,
                input=input,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except OSError, subprocess.TimeoutExpired:
            raise RuntimeError("Test Docker operation could not complete.") from None
        self.assert_safe(result.stdout + result.stderr)
        if check and result.returncode:
            raise RuntimeError("Test Docker operation failed:\n" + result.stderr)
        return result

    def psql(self, sql: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.docker(
            "exec",
            "-i",
            self.container,
            "psql",
            "-X",
            "-U",
            "postgres",
            "-d",
            "financeiq",
            "-v",
            "ON_ERROR_STOP=1",
            input=sql,
            check=check,
        )

    def bootstrap(self, *, check: bool = False) -> subprocess.CompletedProcess[str]:
        return self.psql(BOOTSTRAP.read_text(encoding="utf-8"), check=check)

    def url(self, role: Role, *, sqlalchemy: bool = False) -> str:
        scheme = "postgresql+asyncpg" if sqlalchemy else "postgresql"
        password = quote(self.passwords[role], safe="")
        return f"{scheme}://{role}:{password}@127.0.0.1:{self.port}/financeiq"

    def alembic(
        self,
        *arguments: str,
        role: Role = "financeiq_migrator",
        config: Path | None = None,
        env_file: Path | None = None,
        debug: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            k: v
            for k, v in os.environ.items()
            if not k.upper().startswith("FINANCEIQ_")
        }
        if env_file is None:
            environment["FINANCEIQ_MIGRATION_DATABASE_URL"] = self.url(
                role, sqlalchemy=True
            )
        command = [sys.executable, "-m", "alembic"]
        if debug:
            command = [
                sys.executable,
                "-c",
                "import logging; logging.basicConfig(level=logging.DEBUG); "
                "from alembic.config import main; main()",
            ]
        if config is not None:
            command.extend(["-c", str(config)])
        if env_file is not None:
            command.extend(["-x", f"env_file={env_file}"])
        try:
            result = subprocess.run(
                [*command, *arguments],
                cwd=BACKEND,
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except OSError, subprocess.TimeoutExpired:
            raise RuntimeError("Test migration process could not complete.") from None
        self.assert_safe(result.stdout + result.stderr)
        return result
