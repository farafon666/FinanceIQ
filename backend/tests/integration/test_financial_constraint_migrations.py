"""Financial migration preflights, ACL and data-preserving downgrades."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from financeiq.core.config import MigrationSettings, Settings
from financeiq.db.models import metadata
from financeiq.db.session import create_engine, create_migration_engine
from tests.support.financial import (
    Book,
    archive_account,
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


def test_financial_indexes_roundtrip_preserves_history(
    constraint_database: TestPostgres,
) -> None:
    database = constraint_database
    result = database.alembic("downgrade", "0008")
    assert result.returncode == 0, result.stderr
    assert database.alembic("current").stdout.strip() == "0008"

    async def seed(connection: AsyncConnection) -> None:
        original = await book(connection, ("-100.00", "-100.00"))
        await linked_book(connection, original)
        cancelled = await book(
            connection, owner=original.owner, account=original.account
        )
        await cancel(connection, cancelled)

    command(database, seed)
    data = snapshot(database)
    index_sql = "SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY indexname"
    previous_indexes = query(database, index_sql)
    for direction, target in (
        ("upgrade", "0009"),
        ("downgrade", "0008"),
        ("upgrade", "0009"),
    ):
        result = database.alembic(direction, target)
        assert result.returncode == 0, result.stderr
        assert snapshot(database) == data
        indexes = query(database, index_sql)
        if target == "0008":
            assert indexes == previous_indexes
        else:
            assert len(indexes) == len(previous_indexes) + 15
            assert all(index in indexes for index in previous_indexes)
            check = database.alembic("check")
            assert check.returncode == 0, check.stderr


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


def test_owner_lock_revision_preserves_history_and_trigger_contract(
    constraint_database: TestPostgres,
) -> None:
    database = constraint_database
    assert database.alembic("downgrade", "0007").returncode == 0

    async def seed(connection: AsyncConnection) -> Book:
        value = await book(connection)
        await confirmation(connection, value)
        await linked_book(connection, value)
        return value

    value = command(database, seed)

    async def archive_existing_data(connection: AsyncConnection) -> None:
        category_id = uuid4()
        parameters = {"category": category_id, "owner": value.owner, "rule": uuid4()}
        await connection.execute(
            text(
                "INSERT INTO categories(id,user_id,name,name_key,kind) "
                "VALUES (:category,:owner,'Synthetic','synthetic','expense')"
            ),
            parameters,
        )
        await connection.execute(
            text(
                "INSERT INTO categorization_rules(id,user_id,category_id,pattern,priority) "
                "VALUES (:rule,:owner,:category,'x',10)"
            ),
            parameters,
        )
        await classify(
            connection,
            value,
            value.facts[0],
            kind="set_category",
            changes={"category_id": category_id, "category_origin": "manual"},
        )
        await connection.execute(
            text(
                "UPDATE categories SET archived_at=clock_timestamp() WHERE id=:category"
            ),
            parameters,
        )
        await archive_account(connection, value)

    command(database, archive_existing_data)
    data = snapshot(database)
    trigger_sql = """
        SELECT c.relname, pg_get_triggerdef(t.oid), t.tgenabled FROM pg_trigger t
        JOIN pg_class c ON c.oid=t.tgrelid
        WHERE NOT t.tgisinternal AND t.tgname<>'a_financial_owner_lock'
        ORDER BY c.relname,t.tgname
    """
    legacy_triggers = query(database, trigger_sql)
    grants_sql = """
        SELECT c.relname, a.attname, c.relacl::text, a.attacl::text
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0
        WHERE n.nspname='public' AND c.relkind='r' ORDER BY c.relname,a.attnum
    """
    legacy_grants = query(database, grants_sql)

    for target in ("0008", "0007", "0008"):
        direction = "upgrade" if target == "0008" else "downgrade"
        assert database.alembic(direction, target).returncode == 0
        assert snapshot(database) == data
        assert query(database, trigger_sql) == legacy_triggers
        assert query(database, grants_sql) == legacy_grants
        guards = query(
            database,
            """
            SELECT c.relname, t.tgtype, t.tgenabled::text, p.proname FROM pg_trigger t
            JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_proc p ON p.oid=t.tgfoid
            WHERE NOT t.tgisinternal AND t.tgname='a_financial_owner_lock'
            """,
        )
        if target == "0007":
            assert guards == []
            assert query(
                database,
                "SELECT to_regprocedure('public.lock_financial_owner(uuid)') IS NULL",
            )[0][0]
        else:
            assert len(guards) == 15
            assert {guard[0] for guard in guards} == set(metadata.tables) - {
                "users",
                "auth_sessions",
            }
            # Flags for row-level BEFORE INSERT/UPDATE/DELETE.
            assert all(
                tuple(guard)[1:] == (31, "O", "guard_financial_owner_lock")
                for guard in guards
            )

    functions = query(
        database,
        """
        SELECT p.proname,p.prosecdef,p.provolatile::text,p.proconfig,
            has_function_privilege('financeiq_runtime',p.oid,'EXECUTE'),
            EXISTS(SELECT 1 FROM aclexplode(p.proacl) WHERE grantee=0 AND privilege_type='EXECUTE')
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND p.proname IN (
            'guard_financial_owner_lock','lock_financial_owner','lock_financial_import_batches'
        )
        """,
    )
    assert {row[0]: row[4] for row in functions} == {
        "guard_financial_owner_lock": False,
        "lock_financial_owner": True,
        "lock_financial_import_batches": True,
    }
    for _, definer, volatility, config, _, public_execute in functions:
        assert (
            definer
            and volatility == "v"
            and config == ["search_path=pg_catalog, pg_temp"]
        )
        assert not public_execute


@pytest.mark.parametrize("conflict", ["archived_rule", "refund_total"])
def test_owner_lock_upgrade_rejects_committed_legacy_races(
    constraint_database: TestPostgres, conflict: str
) -> None:
    database = constraint_database
    assert database.alembic("downgrade", "0007").returncode == 0

    async def reproduce() -> None:
        engine = create_engine(
            Settings(
                app_env="test",
                database_url=database.url("financeiq_runtime", sqlalchemy=True),
            )
        )
        try:
            async with engine.begin() as connection:
                value = await book(connection, ("-100.00", "60.00", "60.00"))
                category_id = uuid4()
                await connection.execute(
                    text(
                        "INSERT INTO categories(id,user_id,name,name_key,kind) "
                        "VALUES (:id,:owner,'Synthetic','synthetic','expense')"
                    ),
                    {"id": category_id, "owner": value.owner},
                )
            async with asyncio.timeout(5):
                async with engine.begin() as first, engine.begin() as second:
                    if conflict == "archived_rule":
                        await first.execute(
                            text(
                                "UPDATE categories SET archived_at=clock_timestamp() WHERE id=:id"
                            ),
                            {"id": category_id},
                        )
                        await second.execute(
                            text(
                                "INSERT INTO categorization_rules(id,user_id,category_id,pattern,priority) "
                                "VALUES (:id,:owner,:category,'x',10)"
                            ),
                            {
                                "id": uuid4(),
                                "owner": value.owner,
                                "category": category_id,
                            },
                        )
                    else:
                        changes = {
                            "kind": "refund",
                            "refund_of_transaction_id": value.facts[0],
                        }
                        await classify(
                            first,
                            value,
                            value.facts[1],
                            kind="link_refund",
                            changes=changes,
                        )
                        # Each writer validates before it can see the other's refund.
                        await first.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                        await classify(
                            second,
                            value,
                            value.facts[2],
                            kind="link_refund",
                            changes=changes,
                        )
        finally:
            await engine.dispose()

    asyncio.run(reproduce())
    if conflict == "archived_rule":
        assert (
            query(
                database,
                """
            SELECT count(*) FROM categorization_rules r JOIN categories c
                ON c.user_id=r.user_id AND c.id=r.category_id
            WHERE r.is_active AND c.archived_at IS NOT NULL
            """,
            )[0][0]
            == 1
        )
    else:
        assert query(
            database,
            "SELECT sum(amount) FROM transactions WHERE refund_of_transaction_id IS NOT NULL",
        )[0][0] == Decimal("120.00")

    before = snapshot(database)
    result = database.alembic("--raiseerr", "upgrade", "0008")
    assert result.returncode != 0
    assert "Migration operation failed." in result.stderr
    assert snapshot(database) == before
    assert query(database, "SELECT version_num FROM alembic_version")[0][0] == "0007"
    assert not query(
        database, "SELECT 1 FROM pg_trigger WHERE tgname='a_financial_owner_lock'"
    )
    assert not query(
        database,
        """
        SELECT 1 FROM pg_proc WHERE proname IN (
            'guard_financial_owner_lock','lock_financial_owner','lock_financial_import_batches'
        )
        """,
    )


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
