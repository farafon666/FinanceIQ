"""Real PostgreSQL migration/ACL acceptance; Docker is required, never skipped.

Each invocation owns a random --rm container with tmpfs storage. No existing
database is used. Domain financial guards are covered with stage 2, not these technical probes.
"""

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar, Literal, overload

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from tests.support.postgres import Role, TestPostgres

BACKEND = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.postgres


class MigrationIntegrationTests(unittest.TestCase):
    database: ClassVar[TestPostgres]

    @classmethod
    def setUpClass(cls) -> None:
        cls.database = cls.enterClassContext(TestPostgres())

    @overload
    def query(
        self,
        sql: str,
        role: Role = "financeiq_migrator",
        *,
        execute: Literal[False] = False,
    ) -> list[asyncpg.Record]: ...

    @overload
    def query(
        self, sql: str, role: Role = "financeiq_migrator", *, execute: Literal[True]
    ) -> str: ...

    def query(
        self, sql: str, role: Role = "financeiq_migrator", *, execute: bool = False
    ) -> list[asyncpg.Record] | str:
        async def run() -> list[asyncpg.Record] | str:
            connection = await asyncpg.connect(self.database.url(role), timeout=5)
            try:
                if execute:
                    return await connection.execute(sql)
                return await connection.fetch(sql)
            finally:
                await connection.close()

        return asyncio.run(run())

    def test_explicit_file_accepts_encoded_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env.migrations"
            env_file.write_text(
                "FINANCEIQ_MIGRATION_DATABASE_URL="
                + self.database.url("financeiq_migrator", sqlalchemy=True)
                + "\n",
                encoding="utf-8",
            )
            result = self.database.alembic("upgrade", "head", env_file=env_file)
            self.assertEqual(result.returncode, 0, result.stderr)

    def setUp(self) -> None:
        result = self.database.alembic("upgrade", "head")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_history_roundtrip_and_domain_schema(self) -> None:
        self.assertEqual(self.database.alembic("current").stdout.strip(), "0003 (head)")
        self.assertEqual(self.database.alembic("upgrade", "head").returncode, 0)
        self.assertEqual(self.database.alembic("downgrade", "base").returncode, 0)
        self.assertEqual(self.query("SELECT * FROM alembic_version"), [])
        self.assertEqual(self.database.alembic("upgrade", "0001").returncode, 0)
        self.assertIsNone(self.query("SELECT to_regclass('users')")[0][0])
        self.assertIsNone(self.query("SELECT to_regclass('auth_sessions')")[0][0])
        self.assertEqual(self.database.alembic("upgrade", "head").returncode, 0)
        tables = self.query(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        )
        self.assertEqual(
            [row[0] for row in tables],
            [
                "accounts",
                "alembic_version",
                "auth_sessions",
                "balance_confirmations",
                "financial_actions",
                "history_confirmations",
                "users",
            ],
        )
        self.assertEqual(self.database.alembic("check").returncode, 0)

    def test_auth_downgrade_preserves_nonempty_tables(self) -> None:
        self.query(
            """
            INSERT INTO users(id, email, password_hash, timezone)
            VALUES ('00000000-0000-0000-0000-000000000001',
                    'synthetic@example.com', 'synthetic-hash', 'UTC')
        """,
            execute=True,
        )
        self.addCleanup(self.query, "DELETE FROM users", execute=True)
        self.addCleanup(self.query, "DELETE FROM auth_sessions", execute=True)
        for with_session in (False, True):
            if with_session:
                self.query(
                    """
                    INSERT INTO auth_sessions(id, user_id, token_hash, csrf_token_hash, expires_at)
                    VALUES ('00000000-0000-0000-0000-000000000002',
                            '00000000-0000-0000-0000-000000000001',
                            decode(repeat('ab', 32), 'hex'),
                            decode(repeat('cd', 32), 'hex'), now() + interval '1 day')
                """,
                    execute=True,
                )
            result = self.database.alembic("--raiseerr", "downgrade", "0001")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Migration operation failed.", result.stderr)
            self.assertEqual(
                self.query("SELECT version_num FROM alembic_version")[0][0], "0003"
            )
            self.assertEqual(self.query("SELECT count(*) FROM users")[0][0], 1)
            self.assertEqual(
                self.query("SELECT count(*) FROM auth_sessions")[0][0],
                int(with_session),
            )

    def test_bootstrap_is_repeatable_without_resetting_passwords(self) -> None:
        self.assertEqual(self.database.bootstrap().returncode, 0)
        self.assertEqual(self.database.alembic("upgrade", "head").returncode, 0)
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

    def test_accounts_upgrade_and_empty_downgrade_preserve_auth(self) -> None:
        self.assertEqual(self.database.alembic("downgrade", "0002").returncode, 0)
        self.query(
            """
            INSERT INTO users(id,email,password_hash,timezone)
            VALUES ('00000000-0000-0000-0000-000000000001',
                    'upgrade@example.test','synthetic','UTC');
            INSERT INTO auth_sessions(id,user_id,token_hash,csrf_token_hash,expires_at)
            VALUES ('00000000-0000-0000-0000-000000000002',
                    '00000000-0000-0000-0000-000000000001',
                    decode(repeat('ab',32),'hex'),decode(repeat('cd',32),'hex'),
                    now() + interval '1 day');
        """,
            execute=True,
        )
        self.addCleanup(
            self.query, "DELETE FROM auth_sessions; DELETE FROM users", execute=True
        )
        for _ in range(2):
            self.assertEqual(self.database.alembic("upgrade", "head").returncode, 0)
            self.assertEqual(self.database.alembic("check").returncode, 0)
            self.assertEqual(self.database.alembic("downgrade", "0002").returncode, 0)
            self.assertEqual(self.query("SELECT count(*) FROM users")[0][0], 1)
            self.assertEqual(self.query("SELECT count(*) FROM auth_sessions")[0][0], 1)
            self.assertIsNone(self.query("SELECT to_regclass('accounts')")[0][0])
            self.assertIsNone(
                self.query("SELECT to_regprocedure('guard_confirmation_update()')")[0][
                    0
                ]
            )
        self.assertEqual(self.database.alembic("upgrade", "head").returncode, 0)

    def test_accounts_downgrade_refuses_audit_and_financial_history(self) -> None:
        self.query(
            """
            INSERT INTO users(id,email,password_hash,timezone)
            VALUES ('00000000-0000-0000-0000-000000000001',
                    'downgrade@example.test','synthetic','UTC');
        """,
            execute=True,
        )
        self.addCleanup(
            self.query,
            """
            DELETE FROM history_confirmations; DELETE FROM balance_confirmations;
            DELETE FROM financial_actions; DELETE FROM accounts; DELETE FROM users;
        """,
            execute=True,
        )
        # An action can exist without an account; check it independently first.
        self.query(
            """
            INSERT INTO financial_actions(id,user_id,request_id,action_type,
                plan_version,plan_digest,details)
            VALUES ('00000000-0000-0000-0000-000000000003',
                    '00000000-0000-0000-0000-000000000001',
                    '00000000-0000-0000-0000-000000000004',
                    'confirm_balance',1,decode(repeat('ab',32),'hex'),'{}');
        """,
            execute=True,
        )

        def assert_rejected() -> None:
            result = self.database.alembic("--raiseerr", "downgrade", "0002")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Migration operation failed.", result.stderr)
            self.assertEqual(
                self.query("SELECT version_num FROM alembic_version")[0][0], "0003"
            )
            self.assertEqual(
                self.query("SELECT count(*) FROM financial_actions")[0][0], 1
            )
            self.assertEqual(self.database.alembic("check").returncode, 0)

        assert_rejected()
        self.query(
            """
            INSERT INTO accounts(id,user_id,name,currency_code)
            VALUES ('00000000-0000-0000-0000-000000000005',
                    '00000000-0000-0000-0000-000000000001','Synthetic','RUB');
            INSERT INTO balance_confirmations(id,user_id,account_id,balance_date,
                balance,confirmation_action_id)
            VALUES ('00000000-0000-0000-0000-000000000006',
                    '00000000-0000-0000-0000-000000000001',
                    '00000000-0000-0000-0000-000000000005','2026-10-01',0,
                    '00000000-0000-0000-0000-000000000003');
            INSERT INTO history_confirmations(id,user_id,account_id,start_date,end_date,
                observed_facts_version,observed_facts_digest,confirmation_action_id)
            VALUES ('00000000-0000-0000-0000-000000000007',
                    '00000000-0000-0000-0000-000000000001',
                    '00000000-0000-0000-0000-000000000005','2026-10-01','2026-10-01',
                    0,decode(repeat('cd',32),'hex'),
                    '00000000-0000-0000-0000-000000000003');
        """,
            execute=True,
        )
        assert_rejected()
        for table in ("accounts", "balance_confirmations", "history_confirmations"):
            self.assertEqual(self.query(f"SELECT count(*) FROM {table}")[0][0], 1)

    def test_bootstrap_rejects_incompatible_role(self) -> None:
        self.database.psql("ALTER ROLE financeiq_runtime CREATEDB;")
        try:
            self.assertNotEqual(self.database.bootstrap().returncode, 0)
        finally:
            self.database.psql("ALTER ROLE financeiq_runtime NOCREATEDB;")

    def test_bootstrap_rejects_automatic_runtime_table_grants(self) -> None:
        self.query(
            "ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO financeiq_runtime",
            execute=True,
        )
        try:
            self.assertNotEqual(self.database.bootstrap().returncode, 0)
        finally:
            self.query(
                "ALTER DEFAULT PRIVILEGES REVOKE SELECT ON TABLES FROM financeiq_runtime",
                execute=True,
            )

    def test_runtime_cannot_migrate_or_access_version(self) -> None:
        result = self.database.alembic(
            "--raiseerr", "upgrade", "head", role="financeiq_runtime"
        )
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
            head = ScriptDirectory.from_config(
                Config(str(BACKEND / "alembic.ini"))
            ).get_current_head()
            (root / "migrations" / "versions" / "test_failure.py").write_text(
                f"from alembic import op\nrevision='test_failure'\ndown_revision={head!r}\n"
                "def upgrade():\n"
                "    op.execute('CREATE TABLE rollback_probe(id integer)')\n"
                "    op.execute(\"SELECT 'secret-sentinel'::integer\")\n"
                "def downgrade():\n    pass\n",
                encoding="utf-8",
            )
            result = self.database.alembic(
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
            self.query("SELECT version_num FROM alembic_version")[0][0], "0003"
        )
