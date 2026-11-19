"""Categories storage on migrated disposable PostgreSQL, using real commits."""

import asyncio
from collections.abc import AsyncIterator
from typing import Literal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from financeiq.core.config import MigrationSettings, Settings
from financeiq.db.session import (
    create_engine,
    create_migration_engine,
    create_session_factory,
)
from financeiq.modules.auth.models import User
from financeiq.modules.categories.domain import normalize_category_name
from financeiq.modules.categories.models import CategorizationRule, Category
from tests.support.postgres import TestPostgres

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]
Table = Literal["categories", "categorization_rules"]
COLUMNS: dict[Table, tuple[str, ...]] = {
    "categories": (
        "id",
        "created_at",
        "user_id",
        "name",
        "name_key",
        "kind",
        "archived_at",
    ),
    "categorization_rules": (
        "id",
        "created_at",
        "user_id",
        "category_id",
        "pattern",
        "priority",
        "is_active",
    ),
}


@pytest_asyncio.fixture
async def categories_migrator(
    runtime_postgres: TestPostgres,
) -> AsyncIterator[AsyncEngine]:
    engine = create_migration_engine(
        MigrationSettings(
            migration_database_url=runtime_postgres.url(
                "financeiq_migrator", sqlalchemy=True
            )
        )
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def categories_engine(
    runtime_postgres: TestPostgres, categories_migrator: AsyncEngine
) -> AsyncIterator[AsyncEngine]:
    async def clear() -> None:
        # Only this TestPostgres instance, never a configured application database.
        async with categories_migrator.begin() as connection:
            for table in (
                "categorization_rules",
                "categories",
                "auth_sessions",
                "users",
            ):
                await connection.execute(text(f"DELETE FROM {table}"))

    runtime = create_engine(
        Settings(
            app_env="test",
            database_url=runtime_postgres.url("financeiq_runtime", sqlalchemy=True),
        )
    )
    try:
        await clear()
        yield runtime
    finally:
        await runtime.dispose()
        await clear()


async def test_orm_roundtrip_defaults(categories_engine: AsyncEngine) -> None:
    factory = create_session_factory(categories_engine)
    async with factory.begin() as session:
        owner = User(
            email="categories@example.test", password_hash="synthetic", timezone="UTC"
        )
        session.add(owner)
        await session.flush()
        name, key = normalize_category_name(" КАФЕ\u00a0и рестораны ")
        category = Category(user_id=owner.id, name=name, name_key=key, kind="expense")
        session.add(category)
        await session.flush()
        rule = CategorizationRule(
            user_id=owner.id, category_id=category.id, pattern="Кафе", priority=10
        )
        session.add(rule)
        await session.flush()
        assert category.archived_at is None
        assert rule.is_active is True
        assert rule.active_priority == 10
        category_id, rule_id = category.id, rule.id
    async with factory.begin() as session:
        loaded_category = await session.get(Category, category_id)
        loaded_rule = await session.get(CategorizationRule, rule_id)
        assert loaded_category is not None and loaded_rule is not None
        assert (loaded_category.name, loaded_category.name_key) == (
            "КАФЕ и рестораны",
            "кафе и рестораны",
        )
        assert loaded_category.created_at.tzinfo is not None
        assert loaded_rule.created_at.tzinfo is not None
        assert loaded_rule.category_id == category_id


async def add_owner(connection: AsyncConnection) -> UUID:
    identity = uuid4()
    await connection.execute(
        text(
            "INSERT INTO users(id,email,password_hash,timezone) VALUES (:id,:email,'synthetic','UTC')"
        ),
        {"id": identity, "email": f"{identity.hex}@example.test"},
    )
    return identity


async def add_category(
    connection: AsyncConnection, owner: UUID, name: str = "Кафе", kind: str = "expense"
) -> UUID:
    identity = uuid4()
    display, key = normalize_category_name(name)
    await connection.execute(
        text(
            "INSERT INTO categories(id,user_id,name,name_key,kind) VALUES (:id,:owner,:name,:key,:kind)"
        ),
        {"id": identity, "owner": owner, "name": display, "key": key, "kind": kind},
    )
    return identity


async def add_rule(
    connection: AsyncConnection,
    owner: UUID,
    category: UUID,
    priority: int = 10,
    active: bool = True,
) -> UUID:
    identity = uuid4()
    await connection.execute(
        text(
            "INSERT INTO categorization_rules(id,user_id,category_id,pattern,priority,is_active) VALUES (:id,:owner,:category,'Кафе',:priority,:active)"
        ),
        {
            "id": identity,
            "owner": owner,
            "category": category,
            "priority": priority,
            "active": active,
        },
    )
    return identity


@pytest_asyncio.fixture
async def seeded(categories_engine: AsyncEngine) -> tuple[UUID, UUID, UUID]:
    async with categories_engine.begin() as connection:
        owner = await add_owner(connection)
        category = await add_category(connection, owner)
        rule = await add_rule(connection, owner, category)
    return owner, category, rule


@pytest.mark.parametrize(
    ("table", "overrides", "state"),
    [
        ("categories", {"name": "''"}, "23514"),
        ("categories", {"name": "repeat('я',101)"}, "23514"),
        ("categories", {"name": "NULL"}, "23502"),
        ("categories", {"name_key": "''"}, "23514"),
        ("categories", {"name_key": "'без категории'"}, "23514"),
        ("categories", {"name_key": "NULL"}, "23502"),
        ("categories", {"kind": "'refund'"}, "23514"),
        ("categories", {"kind": "'Expense'"}, "23514"),
        ("categories", {"kind": "NULL"}, "23502"),
        ("categories", {"user_id": "NULL"}, "23502"),
        ("categories", {"user_id": "'00000000-0000-0000-0000-000000000000'"}, "23503"),
        ("categories", {"created_at": "'infinity'"}, "23514"),
        ("categories", {"archived_at": "'-infinity'"}, "23514"),
        ("categories", {"archived_at": "'infinity'"}, "23514"),
        ("categories", {"archived_at": "created_at - interval '1 second'"}, "23514"),
        ("categorization_rules", {"pattern": "''"}, "23514"),
        ("categorization_rules", {"pattern": "repeat('я',201)"}, "23514"),
        ("categorization_rules", {"pattern": "NULL"}, "23502"),
        ("categorization_rules", {"priority": "0"}, "23514"),
        ("categorization_rules", {"priority": "-1"}, "23514"),
        ("categorization_rules", {"priority": "2147483648"}, "22003"),
        ("categorization_rules", {"priority": "NULL"}, "23502"),
        ("categorization_rules", {"category_id": "NULL"}, "23502"),
        (
            "categorization_rules",
            {"category_id": "'00000000-0000-0000-0000-000000000000'"},
            "23503",
        ),
        ("categorization_rules", {"user_id": "NULL"}, "23502"),
        ("categorization_rules", {"is_active": "NULL"}, "23502"),
        ("categorization_rules", {"created_at": "'-infinity'"}, "23514"),
    ],
)
async def test_storage_constraints(
    categories_migrator: AsyncEngine,
    seeded: tuple[UUID, UUID, UUID],
    table: Table,
    overrides: dict[str, str],
    state: str,
) -> None:
    # INSERT tests CHECKs independently of UPDATE guards and runtime column grants.
    expressions = {
        "id": ":new_id",
        **({"name_key": ":key"} if table == "categories" else {"is_active": "false"}),
        **overrides,
    }
    async with categories_migrator.begin() as connection:
        with pytest.raises(DBAPIError) as caught:
            await connection.execute(
                text(
                    f"INSERT INTO {table} ({', '.join(COLUMNS[table])}) SELECT {', '.join(expressions.get(column, column) for column in COLUMNS[table])} FROM {table} WHERE id=:id"
                ),
                {
                    "id": seeded[1 if table == "categories" else 2],
                    "new_id": uuid4(),
                    "key": uuid4().hex,
                },
            )
        assert getattr(caught.value.orig, "sqlstate", None) == state


@pytest.mark.parametrize("name", ["я", "я" * 100, "ß" * 100])
async def test_valid_name_boundaries(categories_engine: AsyncEngine, name: str) -> None:
    async with categories_engine.begin() as connection:
        owner = await add_owner(connection)
        category = await add_category(connection, owner, name)
        row = (
            await connection.execute(
                text("SELECT name,name_key FROM categories WHERE id=:id"),
                {"id": category},
            )
        ).one()
        assert tuple(row) == normalize_category_name(name)


@pytest.mark.parametrize("priority", [1, 2147483647])
async def test_priority_and_pattern_boundaries(
    categories_engine: AsyncEngine, priority: int
) -> None:
    async with categories_engine.begin() as connection:
        owner = await add_owner(connection)
        category = await add_category(connection, owner)
        rule = await add_rule(connection, owner, category, priority)
        await connection.execute(
            text("UPDATE categorization_rules SET pattern=:pattern WHERE id=:id"),
            {"pattern": "я" * 200, "id": rule},
        )
        assert (
            await connection.scalar(
                text("SELECT active_priority FROM categorization_rules WHERE id=:id"),
                {"id": rule},
            )
            == priority
        )


async def test_category_identity_archive_and_restore(
    categories_engine: AsyncEngine, seeded: tuple[UUID, UUID, UUID]
) -> None:
    owner, category, _ = seeded
    async with categories_engine.begin() as connection:
        other = await add_owner(connection)
        await add_category(connection, other, "КАФЕ")
        await add_category(connection, owner, "КАФЕ", "income")
    with pytest.raises(IntegrityError):
        async with categories_engine.begin() as connection:
            await add_category(connection, owner, "\tКАФЕ\u00a0")
    async with categories_engine.begin() as connection:
        await connection.execute(
            text("UPDATE categorization_rules SET is_active=false WHERE id=:id"),
            {"id": seeded[2]},
        )
        await connection.execute(
            text("UPDATE categories SET archived_at=clock_timestamp() WHERE id=:id"),
            {"id": category},
        )
        replacement = await add_category(connection, owner, "КАФЕ")
    with pytest.raises(IntegrityError):
        async with categories_engine.begin() as connection:
            await connection.execute(
                text("UPDATE categories SET archived_at=NULL WHERE id=:id"),
                {"id": category},
            )
    async with categories_engine.begin() as connection:
        name, key = normalize_category_name("Кафе у дома")
        await connection.execute(
            text(
                "UPDATE categories SET name=:name,name_key=:key,archived_at=NULL WHERE id=:id"
            ),
            {"name": name, "key": key, "id": category},
        )
        assert (
            await connection.scalar(
                text("SELECT category_id FROM categorization_rules WHERE id=:id"),
                {"id": seeded[2]},
            )
            == category
        )
        assert (
            await connection.scalar(
                text("SELECT is_active FROM categorization_rules WHERE id=:id"),
                {"id": seeded[2]},
            )
            is False
        )
        assert (
            await connection.scalar(
                text("SELECT archived_at FROM categories WHERE id=:id"),
                {"id": category},
            )
            is None
        )
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM categories WHERE id IN (:first,:second)"),
                {"first": category, "second": replacement},
            )
            == 2
        )


async def test_kind_is_immutable_even_for_migrator(
    categories_migrator: AsyncEngine, seeded: tuple[UUID, UUID, UUID]
) -> None:
    async with categories_migrator.begin() as connection:
        await connection.execute(
            text("UPDATE categories SET kind=kind WHERE id=:id"), {"id": seeded[1]}
        )
    async with categories_migrator.begin() as connection:
        with pytest.raises(IntegrityError) as caught:
            await connection.execute(
                text("UPDATE categories SET kind='income' WHERE id=:id"),
                {"id": seeded[1]},
            )
        assert getattr(caught.value.orig, "sqlstate", None) == "23514"


async def test_rule_rejects_foreign_category(
    categories_engine: AsyncEngine, seeded: tuple[UUID, UUID, UUID]
) -> None:
    async with categories_engine.begin() as connection:
        other = await add_owner(connection)
        category = await add_category(connection, other)
    with pytest.raises(IntegrityError) as caught:
        async with categories_engine.begin() as connection:
            await add_rule(connection, seeded[0], category, 20)
    assert getattr(caught.value.orig, "sqlstate", None) == "23503"
    with pytest.raises(IntegrityError):
        async with categories_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE categorization_rules SET category_id=:category WHERE id=:id"
                ),
                {"category": category, "id": seeded[2]},
            )


async def test_priority_scope_and_generated_values(
    categories_engine: AsyncEngine, seeded: tuple[UUID, UUID, UUID]
) -> None:
    owner, category, _ = seeded
    async with categories_engine.begin() as connection:
        inactive = await add_rule(connection, owner, category, active=False)
        await add_rule(connection, owner, category, active=False)
        other = await add_owner(connection)
        other_category = await add_category(connection, other)
        await add_rule(connection, other, other_category)
        income = await add_category(connection, owner, "Зарплата", "income")
        assert (
            await connection.scalar(
                text("SELECT active_priority FROM categorization_rules WHERE id=:id"),
                {"id": inactive},
            )
            is None
        )
    # Uniqueness spans both directions, and is checked at commit, not INSERT.
    with pytest.raises(IntegrityError):
        async with categories_engine.begin() as connection:
            await add_rule(connection, owner, income)
    with pytest.raises(IntegrityError):
        async with categories_engine.begin() as connection:
            await connection.execute(
                text("UPDATE categorization_rules SET is_active=true WHERE id=:id"),
                {"id": inactive},
            )
    async with categories_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE categorization_rules SET priority=20,is_active=true WHERE id=:id"
            ),
            {"id": inactive},
        )
        assert (
            await connection.scalar(
                text("SELECT active_priority FROM categorization_rules WHERE id=:id"),
                {"id": inactive},
            )
            == 20
        )
        await connection.execute(
            text("UPDATE categorization_rules SET is_active=false WHERE id=:id"),
            {"id": inactive},
        )
        assert (
            await connection.scalar(
                text("SELECT active_priority FROM categorization_rules WHERE id=:id"),
                {"id": inactive},
            )
            is None
        )


async def test_swap_and_invalid_plan_roll_back_together(
    categories_engine: AsyncEngine, seeded: tuple[UUID, UUID, UUID]
) -> None:
    owner, category, first = seeded
    async with categories_engine.begin() as connection:
        second = await add_rule(connection, owner, category, 20)
    async with categories_engine.begin() as connection:
        # Two statements deliberately pass through an intermediate duplicate.
        await connection.execute(
            text("UPDATE categorization_rules SET priority=20 WHERE id=:id"),
            {"id": first},
        )
        await connection.execute(
            text("UPDATE categorization_rules SET priority=10 WHERE id=:id"),
            {"id": second},
        )

    async def read_priorities() -> list[tuple[UUID, int]]:
        async with categories_engine.connect() as connection:
            return [
                (row.id, row.priority)
                for row in await connection.execute(
                    text(
                        "SELECT id,priority FROM categorization_rules ORDER BY priority"
                    )
                )
            ]

    assert await read_priorities() == [(second, 10), (first, 20)]
    async with categories_engine.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(
            text("UPDATE categories SET name='Changed' WHERE id=:id"), {"id": category}
        )
        await connection.execute(
            text("UPDATE categorization_rules SET priority=10 WHERE id=:id"),
            {"id": first},
        )
        with pytest.raises(IntegrityError) as caught:
            await transaction.commit()
        assert getattr(caught.value.orig, "sqlstate", None) == "23505"
        await connection.rollback()
    assert await read_priorities() == [(second, 10), (first, 20)]
    async with categories_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT name FROM categories WHERE id=:id"), {"id": category}
            )
            == "Кафе"
        )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE categories SET id=id",
        "UPDATE categories SET user_id=user_id",
        "UPDATE categories SET created_at=created_at",
        "UPDATE categories SET kind=kind",
        "UPDATE categorization_rules SET id=id",
        "UPDATE categorization_rules SET user_id=user_id",
        "UPDATE categorization_rules SET created_at=created_at",
        "UPDATE categorization_rules SET active_priority=DEFAULT",
        "DELETE FROM categories",
        "DELETE FROM categorization_rules",
        "TRUNCATE categories, categorization_rules",
        "ALTER TABLE categories DISABLE TRIGGER guard_category_kind_update",
        "DROP TABLE categorization_rules",
        "CREATE TABLE categories_probe(id integer)",
        "INSERT INTO categories(id,user_id,name,name_key,kind,created_at) SELECT :id,user_id,'New','new','expense',now() FROM categories LIMIT 1",
        "INSERT INTO categories(id,user_id,name,name_key,kind,archived_at) SELECT :id,user_id,'New','new','expense',NULL FROM categories LIMIT 1",
        "INSERT INTO categorization_rules(id,user_id,category_id,pattern,priority,created_at) SELECT :id,user_id,category_id,'x',20,now() FROM categorization_rules LIMIT 1",
        "INSERT INTO categorization_rules(id,user_id,category_id,pattern,priority,active_priority) VALUES (:id,:owner,:category,'x',20,DEFAULT)",
    ],
)
async def test_runtime_rejects_protected_operations(
    categories_engine: AsyncEngine, seeded: tuple[UUID, UUID, UUID], sql: str
) -> None:
    async with categories_engine.begin() as connection:
        with pytest.raises(DBAPIError) as caught:
            await connection.execute(
                text(sql), {"id": uuid4(), "owner": seeded[0], "category": seeded[1]}
            )
        assert getattr(caught.value.orig, "sqlstate", None) == "42501"


async def test_runtime_column_grants(
    categories_engine: AsyncEngine, seeded: tuple[UUID, UUID, UUID]
) -> None:
    inserts = {
        "categories": {"id", "user_id", "name", "name_key", "kind"},
        "categorization_rules": {
            "id",
            "user_id",
            "category_id",
            "pattern",
            "priority",
            "is_active",
        },
    }
    updates = {
        "categories": {"name", "name_key", "archived_at"},
        "categorization_rules": {"category_id", "pattern", "priority", "is_active"},
    }
    async with categories_engine.connect() as connection:
        for table, columns in COLUMNS.items():
            for column in (
                *columns,
                *(("active_priority",) if table == "categorization_rules" else ()),
            ):
                for privilege, allowed in (
                    ("INSERT", inserts[table]),
                    ("UPDATE", updates[table]),
                ):
                    assert await connection.scalar(
                        text(
                            "SELECT has_column_privilege(current_user,:table,:column,:privilege)"
                        ),
                        {"table": table, "column": column, "privilege": privilege},
                    ) == (column in allowed)
            assert (
                await connection.scalar(
                    text("SELECT has_table_privilege(current_user,:table,'SELECT')"),
                    {"table": table},
                )
                is True
            )
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert (
                    await connection.scalar(
                        text(
                            "SELECT has_table_privilege(current_user,:table,:privilege)"
                        ),
                        {"table": table, "privilege": privilege},
                    )
                    is False
                )


async def test_runtime_can_edit_rule_and_category(
    categories_engine: AsyncEngine, seeded: tuple[UUID, UUID, UUID]
) -> None:
    async with categories_engine.begin() as connection:
        target = await add_category(connection, seeded[0], "Другой доход", "income")
        await connection.execute(
            text(
                "UPDATE categorization_rules SET category_id=:target,pattern='Новый',priority=99,is_active=false WHERE id=:id"
            ),
            {"target": target, "id": seeded[2]},
        )
        row = (
            await connection.execute(
                text(
                    "SELECT category_id,pattern,priority,is_active,active_priority FROM categorization_rules WHERE id=:id"
                ),
                {"id": seeded[2]},
            )
        ).one()
        assert tuple(row) == (target, "Новый", 99, False, None)
        name, key = normalize_category_name("Новое имя")
        await connection.execute(
            text("UPDATE categories SET name=:name,name_key=:key WHERE id=:id"),
            {"name": name, "key": key, "id": seeded[1]},
        )
        assert (
            await connection.scalar(
                text("SELECT name_key FROM categories WHERE id=:id"), {"id": seeded[1]}
            )
            == key
        )


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM categories WHERE id=:id",
        "UPDATE categories SET id=:new_id WHERE id=:id",
        "DELETE FROM users WHERE id=:owner",
    ],
)
async def test_referenced_resources_are_restricted(
    categories_migrator: AsyncEngine, seeded: tuple[UUID, UUID, UUID], sql: str
) -> None:
    async with categories_migrator.begin() as connection:
        with pytest.raises(IntegrityError) as caught:
            await connection.execute(
                text(sql), {"id": seeded[1], "new_id": uuid4(), "owner": seeded[0]}
            )
        assert getattr(caught.value.orig, "sqlstate", None) == "23001"


@pytest.mark.parametrize("table", ["categories", "categorization_rules"])
async def test_uniqueness_with_competing_transactions(
    categories_engine: AsyncEngine, seeded: tuple[UUID, UUID, UUID], table: Table
) -> None:
    owner, category, _ = seeded
    async with (
        categories_engine.connect() as first,
        categories_engine.connect() as second,
    ):
        first_tx = await first.begin()
        second_tx = await second.begin()
        pid = await second.scalar(text("SELECT pg_backend_pid()"))
        if table == "categories":
            await add_category(first, owner, "Конкурентное имя")
        else:
            await add_rule(first, owner, category, 30)
            await add_rule(second, owner, category, 30)

        async def compete() -> None:
            if table == "categories":
                await add_category(second, owner, "КОНКУРЕНТНОЕ ИМЯ")
            else:
                await second_tx.commit()

        contender = asyncio.create_task(compete())
        try:
            async with asyncio.timeout(5):
                while not await first.scalar(
                    text("SELECT cardinality(pg_blocking_pids(:pid))"), {"pid": pid}
                ):
                    await asyncio.sleep(0.01)
            assert not contender.done()
            await first_tx.commit()
            with pytest.raises(IntegrityError):
                await asyncio.wait_for(contender, timeout=5)
        finally:
            if first_tx.is_active:
                await first_tx.rollback()
            if not contender.done():
                contender.cancel()
                await asyncio.gather(contender, return_exceptions=True)
            await second.rollback()
    async with categories_engine.connect() as connection:
        if table == "categories":
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM categories WHERE name_key='конкурентное имя'"
                    )
                )
                == 1
            )
        else:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM categorization_rules WHERE priority=30")
                )
                == 1
            )
