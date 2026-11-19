"""0007 preflight, transactional DDL, ACL and data-preserving downgrade."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from financeiq.core.config import MigrationSettings
from financeiq.db.models import metadata
from financeiq.db.session import create_migration_engine
from tests.support.financial import (
    Book,
    book,
    cancel,
    classify,
    confirmation,
    linked_book,
)
from tests.support.postgres import TestPostgres

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def constraint_postgres() -> Iterator[TestPostgres]:
    with TestPostgres() as database:
        yield database


@pytest.fixture
def constraint_database(constraint_postgres: TestPostgres) -> Iterator[TestPostgres]:
    database = constraint_postgres
    assert database.alembic("upgrade", "head").returncode == 0
    try:
        yield database
    finally:
        # Fixed metadata names, only in this fixture's own disposable database.
        database.psql(
            "TRUNCATE " + ",".join("public." + name for name in metadata.tables)
        )
        assert database.alembic("upgrade", "head").returncode == 0


def command[T](
    database: TestPostgres, operation: Callable[[AsyncConnection], Awaitable[T]]
) -> T:
    async def run() -> T:
        engine = create_migration_engine(
            MigrationSettings(
                migration_database_url=database.url(
                    "financeiq_migrator", sqlalchemy=True
                )
            )
        )
        try:
            async with engine.begin() as connection:
                return await operation(connection)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def query(database: TestPostgres, sql: str) -> list[asyncpg.Record]:
    async def run() -> list[asyncpg.Record]:
        connection = await asyncpg.connect(database.url("financeiq_migrator"))
        try:
            return await connection.fetch(sql)
        finally:
            await connection.close()

    return asyncio.run(run())


def snapshot(database: TestPostgres) -> dict[str, list[asyncpg.Record]]:
    return {
        name: query(database, f"SELECT to_jsonb(t) FROM public.{name} t ORDER BY id")
        for name in metadata.tables
    }


def test_head_metadata_trigger_timing_and_restricted_functions(
    constraint_database: TestPostgres,
) -> None:
    database = constraint_database
    assert database.alembic("check").returncode == 0
    assert database.alembic("upgrade", "head").returncode == 0
    rows = query(
        database,
        "SELECT tgname,tgdeferrable,tginitdeferred FROM pg_trigger WHERE NOT tgisinternal AND tgname LIKE 'financial_%'",
    )
    deferred = {row[0] for row in rows if row[1] and row[2]}
    assert {
        "financial_fact_event",
        "financial_transfer_event",
        "financial_import_event",
        "financial_confirmation_event",
        "financial_action_event",
        "financial_account_event",
        "financial_rule_event",
    } <= deferred
    for row in rows:
        if row[0] in (
            "financial_prepare_fact",
            "financial_fact_effects",
            "financial_guard_action",
            "financial_category_archive",
        ):
            assert not row[1] and not row[2]
    functions = query(
        database,
        "SELECT prosecdef,proconfig,has_function_privilege('financeiq_runtime',p.oid,'EXECUTE') FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND proname LIKE 'financial_%'",
    )
    assert functions
    for row in functions:
        assert row[0] and "search_path=pg_catalog, pg_temp" in row[1] and not row[2]


@pytest.mark.parametrize("cancelled", [False, True])
def test_populated_downgrade_and_upgrade_preserve_valid_history(
    constraint_database: TestPostgres, cancelled: bool
) -> None:
    database = constraint_database

    async def seed(connection: AsyncConnection) -> None:
        value = await book(connection, ("-100.00", "30.00"))
        await classify(
            connection,
            value,
            value.facts[1],
            kind="link_refund",
            changes={"kind": "refund", "refund_of_transaction_id": value.facts[0]},
        )
        await confirmation(connection, value)
        await confirmation(connection, value, balance=True)
        if cancelled:
            await cancel(connection, value)

    command(database, seed)
    before = snapshot(database)
    for _ in range(2):
        assert database.alembic("downgrade", "0006").returncode == 0
        assert snapshot(database) == before
        assert not query(
            database, "SELECT 1 FROM pg_proc WHERE proname='financial_prepare_fact'"
        )
        result = database.alembic("upgrade", "head")
        assert result.returncode == 0, result.stderr
        assert snapshot(database) == before
        assert database.alembic("check").returncode == 0


@pytest.mark.parametrize(
    "incompatible",
    [
        "missing_evidence",
        "orphan_fact",
        "active_rule_on_archive",
        "wrong_version",
        "invalid_audit_state",
        "wrong_confirmation",
        "late_link_to_cancelled_fact",
    ],
)
def test_incompatible_legacy_history_aborts_upgrade_atomically(
    constraint_database: TestPostgres, incompatible: str
) -> None:
    database = constraint_database
    cancelled_original: Book | None = None
    if incompatible == "late_link_to_cancelled_fact":

        async def cancelled_history(connection: AsyncConnection) -> Book:
            value = await book(connection)
            await cancel(connection, value)
            return value

        cancelled_original = command(database, cancelled_history)
    if incompatible in ("wrong_version", "wrong_confirmation"):

        async def valid_history(connection: AsyncConnection) -> None:
            value = await book(connection)
            if incompatible == "wrong_confirmation":
                await confirmation(connection, value)

        command(database, valid_history)
    assert database.alembic("downgrade", "0006").returncode == 0
    if incompatible in ("missing_evidence", "orphan_fact"):
        from tests.integration.test_imports import accept, seed
        from tests.integration.test_transactions import create_fact

        async def invalid(connection: AsyncConnection) -> None:
            if incompatible == "missing_evidence":
                value = await seed(connection)
                await accept(connection, value)
            else:
                await create_fact(connection)

        command(database, invalid)
    elif incompatible == "wrong_version":
        database.psql(
            "SET ROLE financeiq_migrator; UPDATE accounts SET facts_version=0;"
        )
    elif incompatible == "wrong_confirmation":
        database.psql(
            "SET ROLE financeiq_migrator; UPDATE financial_actions SET details=jsonb_set(details,'{accepted_plan,parameters,start_date}','\"2026-09-01\"') WHERE action_type='confirm_history';"
        )
    elif incompatible == "invalid_audit_state":

        async def invalid_audit(connection: AsyncConnection) -> None:
            from uuid import uuid4

            from sqlalchemy import text

            value = await book(connection, audit=False)
            await connection.execute(
                text(
                    "INSERT INTO financial_action_transactions(id,user_id,action_id,transaction_id,before_state,after_state) VALUES (:id,:owner,:action,:fact,'{}','{}')"
                ),
                {
                    "id": uuid4(),
                    "owner": value.owner,
                    "action": value.action,
                    "fact": value.facts[0],
                },
            )
            await connection.execute(
                text("UPDATE accounts SET facts_version=1 WHERE id=:id"),
                {"id": value.account},
            )

        command(database, invalid_audit)
    elif incompatible == "late_link_to_cancelled_fact":
        assert cancelled_original is not None

        async def born_cancelled(connection: AsyncConnection) -> None:
            assert cancelled_original is not None
            linked = await linked_book(connection, cancelled_original)
            await cancel(connection, linked)

        command(database, born_cancelled)
    else:

        async def invalid_rule(connection: AsyncConnection) -> None:
            from sqlalchemy import text

            value = await book(connection, accept=False)
            await connection.execute(
                text(
                    "INSERT INTO categories(id,user_id,name,name_key,kind,archived_at) VALUES (:id,:owner,'Archived','archived','expense',now())"
                ),
                {"id": value.action, "owner": value.owner},
            )
            await connection.execute(
                text(
                    "INSERT INTO categorization_rules(id,user_id,category_id,pattern,priority) VALUES (:id,:owner,:id,'x',1)"
                ),
                {"id": value.action, "owner": value.owner},
            )

        command(database, invalid_rule)
    before = snapshot(database)
    result = database.alembic("--raiseerr", "upgrade", "head")
    assert result.returncode != 0
    assert "Migration operation failed." in result.stderr
    assert snapshot(database) == before
    assert query(database, "SELECT version_num FROM alembic_version")[0][0] == "0006"
    assert not query(
        database, "SELECT 1 FROM pg_proc WHERE proname='financial_prepare_fact'"
    )
    assert not query(
        database,
        "SELECT 1 FROM pg_constraint WHERE conname='ck_financial_actions_evidence_v1'",
    )
