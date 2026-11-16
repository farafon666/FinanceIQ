"""Real PostgreSQL migration/ACL acceptance; Docker is required, never skipped.

Each invocation owns a random --rm container with tmpfs storage. No existing
database is used. This suite does not implement the domain tests of task 1.8.
"""

import asyncio
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import asyncpg

BACKEND = Path(__file__).resolve().parents[2]
BOOTSTRAP = BACKEND.parent / "infra" / "postgres" / "bootstrap.sql"
POSTGRES_IMAGE = (
    "postgres:18.7@sha256:"
    "2ea8630df176ad090ec92136dee850397c5af8781e4bcf2e4162593691c61bbc"
)


class MigrationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.container = f"financeiq-1-6-test-{uuid4().hex}"
        cls.passwords = {
            role: secrets.token_hex(24)
            for role in ("postgres", "financeiq_migrator", "financeiq_runtime")
        }
        cls.passwords["financeiq_migrator"] += "@:%"
        environment = dict(os.environ, POSTGRES_PASSWORD=cls.passwords["postgres"])
        cls.docker(
            "run",
            "--detach",
            "--rm",
            "--name",
            cls.container,
            "--label",
            "financeiq.test=1.6",
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
        cls.addClassCleanup(cls.docker, "stop", "--time", "5", cls.container)
        deadline = time.monotonic() + 30
        while True:
            ready = cls.docker(
                "exec",
                cls.container,
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-U",
                "postgres",
                "-d",
                "financeiq",
                check=False,
            )
            if ready.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Test PostgreSQL did not become ready.")
            time.sleep(0.2)
        port = cls.docker("port", cls.container, "5432/tcp").stdout.strip()
        cls.port = int(port.rsplit(":", 1)[1])
        result = cls.bootstrap()
        if result.returncode:
            raise RuntimeError("Test database bootstrap failed: " + result.stderr)
        # Random, test-only hex passwords go through stdin, never command arguments.
        cls.psql(
            "\n".join(
                f"ALTER ROLE {role} PASSWORD '{password}';"
                for role, password in cls.passwords.items()
                if role != "postgres"
            )
        )

    @classmethod
    def docker(cls, *arguments, environment=None, input=None, check=True):
        return subprocess.run(
            ["docker", *arguments],
            env=environment,
            input=input,
            capture_output=True,
            text=True,
            check=check,
            timeout=120,
        )

    @classmethod
    def psql(cls, sql, check=True):
        return cls.docker(
            "exec",
            "-i",
            cls.container,
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

    @classmethod
    def bootstrap(cls):
        return cls.psql(BOOTSTRAP.read_text(encoding="utf-8"), check=False)

    @classmethod
    def url(cls, role, *, sqlalchemy=False):
        scheme = "postgresql+asyncpg" if sqlalchemy else "postgresql"
        password = quote(cls.passwords[role], safe="")
        return f"{scheme}://{role}:{password}@127.0.0.1:{cls.port}/financeiq"

    def query(self, sql, role="financeiq_migrator", *, execute=False):
        async def run():
            connection = await asyncpg.connect(self.url(role), timeout=5)
            try:
                if execute:
                    return await connection.execute(sql)
                return await connection.fetch(sql)
            finally:
                await connection.close()

        return asyncio.run(run())

    def alembic(
        self,
        *arguments,
        role="financeiq_migrator",
        config=None,
        env_file=None,
        debug=False,
    ):
        environment = {
            k: v for k, v in os.environ.items() if not k.startswith("FINANCEIQ_")
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
        result = subprocess.run(
            [*command, *arguments],
            cwd=BACKEND,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        for secret in (*self.passwords.values(), "secret-sentinel"):
            self.assertNotIn(secret, result.stdout + result.stderr)
            self.assertNotIn(quote(secret, safe=""), result.stdout + result.stderr)
        return result

    def test_explicit_file_accepts_encoded_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env.migrations"
            env_file.write_text(
                "FINANCEIQ_MIGRATION_DATABASE_URL="
                + self.url("financeiq_migrator", sqlalchemy=True)
                + "\n",
                encoding="utf-8",
            )
            result = self.alembic("upgrade", "head", env_file=env_file)
            self.assertEqual(result.returncode, 0, result.stderr)

    def setUp(self) -> None:
        result = self.alembic("upgrade", "head")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_history_roundtrip_and_only_version_table(self) -> None:
        self.assertEqual(self.alembic("current").stdout.strip(), "0001 (head)")
        self.assertEqual(self.alembic("upgrade", "head").returncode, 0)
        self.assertEqual(self.alembic("downgrade", "base").returncode, 0)
        self.assertEqual(self.query("SELECT * FROM alembic_version"), [])
        self.assertEqual(self.alembic("upgrade", "head").returncode, 0)
        tables = self.query("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        self.assertEqual([row[0] for row in tables], ["alembic_version"])
        self.assertEqual(self.alembic("check").returncode, 0)

    def test_bootstrap_is_repeatable_without_resetting_passwords(self) -> None:
        self.assertEqual(self.bootstrap().returncode, 0)
        self.assertEqual(self.alembic("upgrade", "head").returncode, 0)
        rows = self.query("""
            SELECT rolname, rolsuper, rolinherit, rolcreatedb, rolcreaterole,
                   rolreplication, rolbypassrls, rolcanlogin
            FROM pg_roles WHERE rolname LIKE 'financeiq_%' ORDER BY rolname
        """)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(tuple(row)[1:], (False,) * 6 + (True,))
        owners = self.query("""
            SELECT pg_get_userbyid(datdba),
                (SELECT pg_get_userbyid(nspowner) FROM pg_namespace
                 WHERE nspname='public'),
                (SELECT pg_get_userbyid(relowner) FROM pg_class
                 WHERE oid='public.alembic_version'::regclass)
            FROM pg_database WHERE datname=current_database()
        """)
        self.assertEqual(
            tuple(owners[0]), ("postgres", "financeiq_migrator", "financeiq_migrator")
        )

    def test_bootstrap_rejects_incompatible_role(self) -> None:
        self.psql("ALTER ROLE financeiq_runtime CREATEDB;")
        try:
            self.assertNotEqual(self.bootstrap().returncode, 0)
        finally:
            self.psql("ALTER ROLE financeiq_runtime NOCREATEDB;")

    def test_bootstrap_rejects_automatic_runtime_table_grants(self) -> None:
        self.query(
            "ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO financeiq_runtime",
            execute=True,
        )
        try:
            self.assertNotEqual(self.bootstrap().returncode, 0)
        finally:
            self.query(
                "ALTER DEFAULT PRIVILEGES REVOKE SELECT ON TABLES FROM financeiq_runtime",
                execute=True,
            )

    def test_runtime_cannot_migrate_or_access_version(self) -> None:
        result = self.alembic("--raiseerr", "upgrade", "head", role="financeiq_runtime")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Migration operation failed.", result.stderr)
        for sql in (
            "SELECT * FROM alembic_version",
            "UPDATE alembic_version SET version_num='bad'",
            "SET ROLE financeiq_migrator",
            "SET session_replication_role=replica",
            "CREATE TABLE forbidden(id integer)",
            "CREATE TEMP TABLE forbidden(id integer)",
            "CREATE SCHEMA forbidden",
            "CREATE DATABASE forbidden",
            "CREATE ROLE forbidden",
        ):
            with (
                self.subTest(sql=sql),
                self.assertRaises(asyncpg.InsufficientPrivilegeError),
            ):
                self.query(sql, "financeiq_runtime", execute=True)

    def test_explicit_dml_grants_preserve_trigger_and_deny_ddl(self) -> None:
        self.query(
            """
            CREATE TABLE acl_probe(id integer PRIMARY KEY, value integer NOT NULL);
            CREATE FUNCTION acl_probe_guard() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN NEW.value := NEW.value + 1; RETURN NEW; END $$;
            CREATE TRIGGER acl_guard BEFORE INSERT OR UPDATE ON acl_probe
            FOR EACH ROW EXECUTE FUNCTION acl_probe_guard();
            GRANT SELECT, INSERT, UPDATE, DELETE ON acl_probe TO financeiq_runtime;
        """,
            execute=True,
        )
        self.addCleanup(
            self.query,
            """
            DROP TABLE acl_probe;
            DROP FUNCTION acl_probe_guard();
        """,
            execute=True,
        )
        self.query(
            "INSERT INTO acl_probe VALUES(1, 10)", "financeiq_runtime", execute=True
        )
        self.assertEqual(
            self.query("SELECT value FROM acl_probe", "financeiq_runtime")[0][0], 11
        )
        self.query("UPDATE acl_probe SET value=20", "financeiq_runtime", execute=True)
        self.assertEqual(
            self.query("SELECT value FROM acl_probe", "financeiq_runtime")[0][0], 21
        )
        self.assertFalse(
            self.query(
                "SELECT has_function_privilege('financeiq_runtime', 'acl_probe_guard()', 'EXECUTE')"
            )[0][0]
        )
        for sql in (
            "TRUNCATE acl_probe",
            "DROP TABLE acl_probe",
            "ALTER TABLE acl_probe ADD COLUMN extra integer",
            "ALTER TABLE acl_probe DISABLE TRIGGER acl_guard",
            "DROP TRIGGER acl_guard ON acl_probe",
        ):
            with (
                self.subTest(sql=sql),
                self.assertRaises(asyncpg.InsufficientPrivilegeError),
            ):
                self.query(sql, "financeiq_runtime", execute=True)
        self.query("DELETE FROM acl_probe", "financeiq_runtime", execute=True)
        self.assertEqual(self.query("SELECT * FROM acl_probe", "financeiq_runtime"), [])

    def test_new_tables_are_not_automatically_granted(self) -> None:
        self.query("CREATE TABLE private_probe(id integer)", execute=True)
        self.addCleanup(self.query, "DROP TABLE private_probe", execute=True)
        with self.assertRaises(asyncpg.InsufficientPrivilegeError):
            self.query("SELECT * FROM private_probe", "financeiq_runtime")

    def test_failing_ddl_is_atomic_and_diagnostics_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(BACKEND / "migrations", root / "migrations")
            (root / "alembic.ini").write_text(
                "[alembic]\nscript_location = %(here)s/migrations\n", encoding="utf-8"
            )
            (root / "migrations" / "versions" / "0002_failure.py").write_text(
                "from alembic import op\nrevision='0002'\ndown_revision='0001'\n"
                "def upgrade():\n"
                "    op.execute('CREATE TABLE rollback_probe(id integer)')\n"
                "    op.execute(\"SELECT 'secret-sentinel'::integer\")\n"
                "def downgrade():\n    pass\n",
                encoding="utf-8",
            )
            result = self.alembic(
                "--raiseerr",
                "upgrade",
                "head",
                config=root / "alembic.ini",
                debug=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Migration operation failed.", result.stderr)
            self.assertNotIn("rollback_probe", result.stderr)
            self.assertNotIn("During handling", result.stderr)
        self.assertIsNone(self.query("SELECT to_regclass('rollback_probe')")[0][0])
        self.assertEqual(
            self.query("SELECT version_num FROM alembic_version")[0][0], "0001"
        )
