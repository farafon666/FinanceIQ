"""Safe Transactions migration transitions on an owned disposable database."""

import asyncio
from collections.abc import Iterator, Set

import asyncpg
import pytest
from sqlalchemy import text

from financeiq.core.config import MigrationSettings
from financeiq.db.session import create_migration_engine
from tests.integration.test_imports import accept, seed
from tests.integration.test_transactions import (
    add_cancellation,
    add_category,
    create_fact,
)
from tests.support.postgres import REVISION_0006_TABLES, TestPostgres

pytestmark = pytest.mark.postgres
NEW_TABLES = {"transactions", "transfer_links", "financial_action_transactions"}


@pytest.fixture(scope="module")
def transactions_postgres() -> Iterator[TestPostgres]:
    with TestPostgres() as database:
        yield database


@pytest.fixture
def migration_database(transactions_postgres: TestPostgres) -> Iterator[TestPostgres]:
    database = transactions_postgres

    def clear() -> None:
        # All names come from the catalog of this fixture's own ephemeral DB.
        database.psql("""
            DO $$ DECLARE targets text; BEGIN
                SELECT string_agg(format('%I.%I',schemaname,tablename),',')
                INTO targets FROM pg_tables
                WHERE schemaname='public' AND tablename <> 'alembic_version';
                IF targets IS NOT NULL THEN EXECUTE 'TRUNCATE ' || targets; END IF;
            END $$;
        """)

    clear()
    result = database.alembic("upgrade", "0006")
    assert result.returncode == 0, result.stderr
    try:
        yield database
    finally:
        clear()
        result = database.alembic("upgrade", "0006")
        assert result.returncode == 0, result.stderr


def query(database: TestPostgres, sql: str) -> list[asyncpg.Record]:
    async def run() -> list[asyncpg.Record]:
        connection = await asyncpg.connect(database.url("financeiq_migrator"))
        try:
            return await connection.fetch(sql)
        finally:
            await connection.close()

    return asyncio.run(run())


def snapshot(
    database: TestPostgres, tables: Set[str]
) -> dict[str, list[asyncpg.Record]]:
    return {
        name: query(
            database,
            f"SELECT to_jsonb(t) - 'result_transaction_id' FROM {name} t ORDER BY id",
        )
        for name in sorted(tables)
    }


async def seed_history(
    database: TestPostgres,
    *,
    accepted: bool = False,
    fact: bool = False,
    cancelled: bool = False,
) -> None:
    engine = create_migration_engine(
        MigrationSettings(
            migration_database_url=database.url("financeiq_migrator", sqlalchemy=True)
        )
    )
    try:
        async with engine.begin() as connection:
            if fact:
                graph, transaction = await create_fact(connection)
                await connection.execute(
                    text(
                        "INSERT INTO financial_action_transactions(id,user_id,action_id,transaction_id,before_state,after_state) VALUES (:action,:owner,:action,:transaction,'{}','{}')"
                    ),
                    graph.parameters() | {"transaction": transaction},
                )
                await connection.execute(
                    text(
                        "INSERT INTO transfer_links(id,user_id,outgoing_transaction_id,confirmation_action_id) VALUES (:action,:owner,:transaction,:action)"
                    ),
                    graph.parameters() | {"transaction": transaction},
                )
                if cancelled:
                    cancellation = await add_cancellation(connection, graph)
                    await connection.execute(
                        text(
                            "UPDATE transactions SET cancelled_by_id=:cancel WHERE id=:transaction"
                        ),
                        {"transaction": transaction, "cancel": cancellation},
                    )
                    await connection.execute(
                        text("UPDATE transfer_links SET cancelled_by_id=:cancel"),
                        {"cancel": cancellation},
                    )
            else:
                graph = await seed(connection)
                if accepted:
                    await accept(connection, graph)
            await add_category(connection, graph.owner)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("accepted", [False, True])
def test_upgrade_and_empty_downgrade_preserve_previous_history(
    migration_database: TestPostgres, accepted: bool
) -> None:
    database = migration_database
    assert database.alembic("downgrade", "0005").returncode == 0
    asyncio.run(seed_history(database, accepted=accepted))
    previous_tables = REVISION_0006_TABLES - NEW_TABLES
    before = snapshot(database, previous_tables)
    for _ in range(2):
        result = database.alembic("upgrade", "0006")
        assert result.returncode == 0, result.stderr
        assert database.alembic("upgrade", "0006").returncode == 0
        assert snapshot(database, previous_tables) == before
        assert (
            query(database, "SELECT result_transaction_id FROM import_movements")[0][0]
            is None
        )
        assert database.alembic("downgrade", "0005").returncode == 0
        assert tuple(
            query(
                database,
                "SELECT to_regclass('transactions'),to_regprocedure('guard_transaction_history()')",
            )[0]
        ) == (None, None)
        assert snapshot(database, previous_tables) == before


def test_incompatible_legacy_link_upgrade_is_atomic(
    migration_database: TestPostgres,
) -> None:
    database = migration_database
    assert database.alembic("downgrade", "0005").returncode == 0
    asyncio.run(seed_history(database))
    database.psql("UPDATE import_movements SET decision='link';")
    previous_tables = REVISION_0006_TABLES - NEW_TABLES
    before = snapshot(database, previous_tables)
    result = database.alembic("--raiseerr", "upgrade", "0006")
    assert result.returncode != 0
    assert "Migration operation failed." in result.stderr
    assert query(database, "SELECT version_num FROM alembic_version")[0][0] == "0005"
    assert snapshot(database, previous_tables) == before
    assert query(database, "SELECT to_regclass('transactions')")[0][0] is None
    assert not query(
        database,
        "SELECT 1 FROM information_schema.columns WHERE table_name='import_movements' AND column_name='result_transaction_id'",
    )


@pytest.mark.parametrize("cancelled", [False, True])
def test_downgrade_preserves_live_and_cancelled_financial_history(
    migration_database: TestPostgres, cancelled: bool
) -> None:
    database = migration_database
    asyncio.run(seed_history(database, fact=True, cancelled=cancelled))
    before = snapshot(database, REVISION_0006_TABLES)
    results = query(database, "SELECT result_transaction_id FROM import_movements")
    result = database.alembic("--raiseerr", "downgrade", "0005")
    assert result.returncode != 0
    assert "Migration operation failed." in result.stderr
    assert query(database, "SELECT version_num FROM alembic_version")[0][0] == "0006"
    assert snapshot(database, REVISION_0006_TABLES) == before
    assert (
        query(database, "SELECT result_transaction_id FROM import_movements") == results
    )


@pytest.mark.parametrize(
    "action_type",
    [
        "set_kind",
        "set_category",
        "confirm_transfer",
        "replace_transfer",
        "unlink_transfer",
        "link_refund",
        "unlink_refund",
        "confirm_unlinked_refund",
    ],
)
def test_downgrade_refuses_new_action_without_elements(
    migration_database: TestPostgres, action_type: str
) -> None:
    database = migration_database
    database.psql(f"""
        INSERT INTO users(id,email,password_hash,timezone)
        VALUES ('00000000-0000-0000-0000-000000000001','synthetic@example.test','synthetic','UTC');
        INSERT INTO financial_actions(id,user_id,request_id,action_type,plan_version,plan_digest,details)
        VALUES ('00000000-0000-0000-0000-000000000002','00000000-0000-0000-0000-000000000001',
                '00000000-0000-0000-0000-000000000002','{action_type}',1,decode(repeat('ab',32),'hex'),'{{}}');
    """)
    before = snapshot(database, {"users", "financial_actions"})
    assert database.alembic("downgrade", "0005").returncode != 0
    assert query(database, "SELECT version_num FROM alembic_version")[0][0] == "0006"
    assert snapshot(database, {"users", "financial_actions"}) == before
