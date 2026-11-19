"""Accounts storage on migrated disposable PostgreSQL, including direct DML."""

import asyncio
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from financeiq.core.config import MigrationSettings, Settings
from financeiq.db.financial_actions import FinancialAction
from financeiq.db.session import (
    create_engine,
    create_migration_engine,
    create_session_factory,
)
from financeiq.modules.accounts.models import (
    Account,
    BalanceConfirmation,
    HistoryConfirmation,
)
from financeiq.modules.auth.models import User
from tests.support.postgres import TestPostgres

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]
ConfirmationTable = Literal["balance_confirmations", "history_confirmations"]
Table = Literal[
    "accounts", "financial_actions", "balance_confirmations", "history_confirmations"
]
COLUMNS: dict[Table, tuple[str, ...]] = {
    "accounts": (
        "id",
        "created_at",
        "user_id",
        "name",
        "currency_code",
        "archived_at",
        "facts_version",
    ),
    "financial_actions": (
        "id",
        "created_at",
        "user_id",
        "request_id",
        "action_type",
        "plan_version",
        "plan_digest",
        "schema_version",
        "account_id",
        "reason_code",
        "reason",
        "details",
    ),
    "balance_confirmations": (
        "id",
        "created_at",
        "user_id",
        "account_id",
        "balance_date",
        "balance",
        "confirmation_action_id",
        "revocation_action_id",
    ),
    "history_confirmations": (
        "id",
        "created_at",
        "user_id",
        "account_id",
        "start_date",
        "end_date",
        "observed_facts_version",
        "observed_facts_digest",
        "confirmation_action_id",
        "revocation_action_id",
    ),
}


@pytest_asyncio.fixture
async def accounts_migrator(
    storage_postgres: TestPostgres,
) -> AsyncIterator[AsyncEngine]:
    engine = create_migration_engine(
        MigrationSettings(
            migration_database_url=storage_postgres.url(
                "financeiq_migrator", sqlalchemy=True
            )
        )
    )
    try:
        yield engine
    finally:
        await engine.dispose()


async def clear_accounts(engine: AsyncEngine) -> None:
    # Only the caller's disposable TestPostgres database, never application data.
    async with engine.begin() as connection:
        for table in (
            "history_confirmations",
            "balance_confirmations",
            "financial_actions",
            "accounts",
            "auth_sessions",
            "users",
        ):
            await connection.execute(text(f"DELETE FROM {table}"))


@pytest_asyncio.fixture
async def accounts_engine(
    storage_postgres: TestPostgres, accounts_migrator: AsyncEngine
) -> AsyncIterator[AsyncEngine]:
    runtime = create_engine(
        Settings(
            app_env="test",
            database_url=storage_postgres.url("financeiq_runtime", sqlalchemy=True),
        )
    )
    try:
        await clear_accounts(accounts_migrator)
        yield runtime
    finally:
        await runtime.dispose()
        await clear_accounts(accounts_migrator)


async def add_owner(connection: AsyncConnection) -> UUID:
    identity = uuid4()
    await connection.execute(
        text(
            "INSERT INTO users(id, email, password_hash, timezone) VALUES (:id, :email, 'synthetic-hash', 'Europe/Saratov')"
        ),
        {"id": identity, "email": f"{identity.hex}@example.test"},
    )
    return identity


async def add_account(connection: AsyncConnection, owner: UUID) -> UUID:
    identity = uuid4()
    await connection.execute(
        text(
            "INSERT INTO accounts(id, user_id, name, currency_code) VALUES (:id, :owner, 'Synthetic account', 'RUB')"
        ),
        {"id": identity, "owner": owner},
    )
    return identity


async def add_action(
    connection: AsyncConnection,
    owner: UUID,
    account: UUID,
    kind: str = "confirm_balance",
) -> UUID:
    identity = uuid4()
    await connection.execute(
        text(
            "INSERT INTO financial_actions(id, user_id, account_id, request_id, action_type, plan_version, plan_digest, details) VALUES (:id, :owner, :account, :request, :kind, 1, :digest, '{}')"
        ),
        {
            "id": identity,
            "owner": owner,
            "account": account,
            "request": uuid4(),
            "kind": kind,
            "digest": b"a" * 32,
        },
    )
    return identity


async def add_confirmation(
    connection: AsyncConnection, table: ConfirmationTable, owner: UUID, account: UUID
) -> UUID:
    identity = uuid4()
    action = await add_action(
        connection,
        owner,
        account,
        "confirm_balance" if table == "balance_confirmations" else "confirm_history",
    )
    fields = (
        "balance_date, balance"
        if table == "balance_confirmations"
        else "start_date, end_date, observed_facts_version, observed_facts_digest"
    )
    values = (
        "'2026-10-01', 0.00"
        if table == "balance_confirmations"
        else "'2026-10-01', '2026-10-03', 0, decode(repeat('ab', 32), 'hex')"
    )
    await connection.execute(
        text(
            f"INSERT INTO {table}(id, user_id, account_id, confirmation_action_id, {fields}) VALUES (:id, :owner, :account, :action, {values})"
        ),
        {"id": identity, "owner": owner, "account": account, "action": action},
    )
    return identity


async def copy_row(
    connection: AsyncConnection, table: Table, identity: UUID, overrides: dict[str, str]
) -> UUID:
    """Trusted SQL expressions from test cases, never external input."""
    new_id = uuid4()
    expressions = {"id": ":new_id", **overrides}
    await connection.execute(
        text(
            f"INSERT INTO {table} ({', '.join(COLUMNS[table])}) SELECT {', '.join(expressions.get(column, column) for column in COLUMNS[table])} FROM {table} WHERE id=:id"
        ),
        {"id": identity, "new_id": new_id},
    )
    return new_id


@pytest_asyncio.fixture
async def seeded(accounts_engine: AsyncEngine) -> dict[Table, UUID]:
    async with accounts_engine.begin() as connection:
        owner = await add_owner(connection)
        account = await add_account(connection, owner)
        action = await add_action(connection, owner, account)
        balance = await add_confirmation(
            connection, "balance_confirmations", owner, account
        )
        history = await add_confirmation(
            connection, "history_confirmations", owner, account
        )
    return {
        "accounts": account,
        "financial_actions": action,
        "balance_confirmations": balance,
        "history_confirmations": history,
    }


async def test_orm_roundtrip_defaults_and_duplicate_names(
    accounts_engine: AsyncEngine,
) -> None:
    factory = create_session_factory(accounts_engine)
    async with factory.begin() as session:
        owner = User(
            email="orm@example.test", password_hash="synthetic", timezone="UTC"
        )
        session.add(owner)
        await session.flush()
        account = Account(user_id=owner.id, name="Счёт 💰", currency_code="RUB")
        session.add(account)
        await session.flush()
        action = FinancialAction(
            user_id=owner.id,
            account_id=account.id,
            request_id=uuid4(),
            action_type="confirm_balance",
            plan_version=1,
            plan_digest=b"p" * 32,
            details={"accepted_plan": {}, "result": {}},
        )
        history_action = FinancialAction(
            user_id=owner.id,
            account_id=account.id,
            request_id=uuid4(),
            action_type="confirm_history",
            plan_version=1,
            plan_digest=b"h" * 32,
            details={},
        )
        session.add_all([action, history_action])
        await session.flush()
        balance = BalanceConfirmation(
            user_id=owner.id,
            account_id=account.id,
            balance_date=date(2026, 10, 1),
            balance=Decimal("-123.45"),
            confirmation_action_id=action.id,
        )
        history = HistoryConfirmation(
            user_id=owner.id,
            account_id=account.id,
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 1),
            observed_facts_version=0,
            observed_facts_digest=b"d" * 32,
            confirmation_action_id=history_action.id,
        )
        session.add_all([balance, history])
        await session.flush()
        ids = account.id, action.id, balance.id, history.id
        assert account.facts_version == 0
        assert account.archived_at is None
        assert action.schema_version == 1
    async with factory.begin() as session:
        loaded_balance = await session.get(BalanceConfirmation, ids[2])
        loaded_history = await session.get(HistoryConfirmation, ids[3])
        loaded_action = await session.get(FinancialAction, ids[1])
        assert (
            loaded_balance is not None
            and loaded_history is not None
            and loaded_action is not None
        )
        assert loaded_balance.balance == Decimal("-123.45")
        assert loaded_balance.revocation_action_id is None
        assert loaded_history.observed_facts_digest == b"d" * 32
        assert loaded_history.start_date == loaded_history.end_date
        assert loaded_action.details == {"accepted_plan": {}, "result": {}}
        assert loaded_balance.created_at.tzinfo is not None
    async with accounts_engine.begin() as connection:
        same = await add_account(connection, owner.id)
        duplicate = await add_account(connection, owner.id)
        other = await add_owner(connection)
        another = await add_account(connection, other)
        assert len({ids[0], same, duplicate, another}) == 4


@pytest.mark.parametrize(
    "value",
    ["0.00", "-0.00", "0.10", "-1.23", "9999999999999999.99", "-9999999999999999.99"],
)
async def test_exact_balance_storage(
    accounts_engine: AsyncEngine,
    accounts_migrator: AsyncEngine,
    seeded: dict[Table, UUID],
    value: str,
) -> None:
    async with accounts_migrator.begin() as connection:
        identity = await copy_row(
            connection,
            "balance_confirmations",
            seeded["balance_confirmations"],
            {"balance_date": "'1900-01-01'", "balance": value},
        )
    async with accounts_engine.connect() as connection:
        assert await connection.scalar(
            text("SELECT balance FROM balance_confirmations WHERE id=:id"),
            {"id": identity},
        ) == Decimal(value)


@pytest.mark.parametrize(
    ("table", "overrides", "state"),
    [
        ("accounts", {"name": "''"}, "23514"),
        ("accounts", {"name": "repeat('я', 101)"}, "23514"),
        ("accounts", {"name": "NULL"}, "23502"),
        ("accounts", {"currency_code": "'USD'"}, "23514"),
        ("accounts", {"currency_code": "'rub'"}, "23514"),
        ("accounts", {"facts_version": "-1"}, "23514"),
        ("accounts", {"archived_at": "created_at - interval '1 second'"}, "23514"),
        ("accounts", {"archived_at": "'infinity'"}, "23514"),
        ("accounts", {"created_at": "'-infinity'"}, "23514"),
        ("financial_actions", {"request_id": "NULL"}, "23502"),
        ("financial_actions", {"action_type": "'SET_KIND'"}, "23514"),
        ("financial_actions", {"action_type": "'unknown'"}, "23514"),
        ("financial_actions", {"plan_version": "0"}, "23514"),
        ("financial_actions", {"schema_version": "0"}, "23514"),
        ("financial_actions", {"plan_digest": "decode('ab', 'hex')"}, "23514"),
        ("financial_actions", {"details": "'[]'"}, "23514"),
        ("financial_actions", {"details": "'null'"}, "23514"),
        ("financial_actions", {"created_at": "'infinity'"}, "23514"),
        ("balance_confirmations", {"balance_date": "'infinity'"}, "23514"),
        ("balance_confirmations", {"balance_date": "'-infinity'"}, "23514"),
        ("balance_confirmations", {"balance": "'NaN'"}, "23514"),
        ("balance_confirmations", {"balance": "'Infinity'"}, "22003"),
        ("balance_confirmations", {"balance": "'-Infinity'"}, "22003"),
        ("balance_confirmations", {"balance": "10000000000000000.00"}, "22003"),
        ("balance_confirmations", {"balance": "NULL"}, "23502"),
        ("balance_confirmations", {"confirmation_action_id": "NULL"}, "23502"),
        ("history_confirmations", {"start_date": "'2026-10-04'"}, "23514"),
        ("history_confirmations", {"start_date": "'-infinity'"}, "23514"),
        ("history_confirmations", {"end_date": "'infinity'"}, "23514"),
        ("history_confirmations", {"observed_facts_version": "-1"}, "23514"),
        (
            "history_confirmations",
            {"observed_facts_digest": "decode('ab', 'hex')"},
            "23514",
        ),
        ("history_confirmations", {"observed_facts_digest": "NULL"}, "23502"),
        ("history_confirmations", {"confirmation_action_id": "NULL"}, "23502"),
        ("history_confirmations", {"created_at": "'infinity'"}, "23514"),
    ],
)
async def test_storage_constraints(
    accounts_migrator: AsyncEngine,
    seeded: dict[Table, UUID],
    table: Table,
    overrides: dict[str, str],
    state: str,
) -> None:
    # Insert a fresh invalid row so immutability guards cannot hide missing CHECKs.
    async with accounts_migrator.begin() as connection:
        with pytest.raises(DBAPIError) as caught:
            await copy_row(connection, table, seeded[table], overrides)
        assert getattr(caught.value.orig, "sqlstate", None) == state


@pytest.mark.parametrize("table", ["balance_confirmations", "history_confirmations"])
@pytest.mark.parametrize(
    "column", ["account_id", "confirmation_action_id", "revocation_action_id"]
)
async def test_confirmation_rejects_foreign_owner(
    accounts_engine: AsyncEngine,
    accounts_migrator: AsyncEngine,
    seeded: dict[Table, UUID],
    table: ConfirmationTable,
    column: str,
) -> None:
    async with accounts_engine.begin() as connection:
        other = await add_owner(connection)
        account = await add_account(connection, other)
        action = await add_action(connection, other, account)
    target = account if column == "account_id" else action
    async with accounts_migrator.begin() as connection:
        with pytest.raises(IntegrityError) as caught:
            await copy_row(
                connection,
                table,
                seeded[table],
                {
                    column: f"'{target}'",
                    **(
                        {"balance_date": "'2026-09-30'"}
                        if table == "balance_confirmations"
                        else {"start_date": "'2026-09-30'"}
                    ),
                },
            )
        assert getattr(caught.value.orig, "sqlstate", None) == "23503"


async def test_action_owner_and_request_identity(
    accounts_engine: AsyncEngine,
    accounts_migrator: AsyncEngine,
    seeded: dict[Table, UUID],
) -> None:
    async with accounts_engine.begin() as connection:
        other = await add_owner(connection)
        other_account = await add_account(connection, other)
    async with accounts_migrator.begin() as connection:
        with pytest.raises(IntegrityError):
            await copy_row(
                connection, "financial_actions", seeded["financial_actions"], {}
            )
    async with accounts_migrator.begin() as connection:
        with pytest.raises(IntegrityError) as caught:
            await copy_row(
                connection,
                "financial_actions",
                seeded["financial_actions"],
                {"account_id": f"'{other_account}'", "request_id": f"'{uuid4()}'"},
            )
        assert getattr(caught.value.orig, "sqlstate", None) == "23503"
    async with accounts_migrator.begin() as connection:
        await copy_row(
            connection,
            "financial_actions",
            seeded["financial_actions"],
            {"user_id": f"'{other}'", "account_id": f"'{other_account}'"},
        )


@pytest.mark.parametrize("table", ["balance_confirmations", "history_confirmations"])
@pytest.mark.parametrize(
    "column", ["account_id", "confirmation_action_id", "revocation_action_id"]
)
async def test_runtime_confirmation_rejects_foreign_reference(
    accounts_engine: AsyncEngine, table: ConfirmationTable, column: str
) -> None:
    async with accounts_engine.begin() as connection:
        owner = await add_owner(connection)
        account = await add_account(connection, owner)
        confirmation = await add_confirmation(connection, table, owner, account)
        action_type = (
            "confirm_balance" if table == "balance_confirmations" else "confirm_history"
        )
        action = await add_action(connection, owner, account, action_type)
        other = await add_owner(connection)
        other_account = await add_account(connection, other)
        other_action = await add_action(connection, other, other_account, action_type)
    fields, values = (
        ("balance_date,balance", "'2026-09-30',0.00")
        if table == "balance_confirmations"
        else (
            "start_date,end_date,observed_facts_version,observed_facts_digest",
            "'2026-09-30','2026-10-03',0,decode(repeat('ab',32),'hex')",
        )
    )
    parameters = {
        "id": uuid4(),
        "owner": owner,
        "account": other_account if column == "account_id" else account,
        "action": other_action if column != "account_id" else action,
        "confirmation": confirmation,
    }
    statement = (
        f"UPDATE {table} SET revocation_action_id=:action WHERE id=:confirmation"
        if column == "revocation_action_id"
        else f"INSERT INTO {table}(id,user_id,account_id,confirmation_action_id,{fields}) "
        f"VALUES (:id,:owner,:account,:action,{values})"
    )
    with pytest.raises(DBAPIError) as failure:
        async with accounts_engine.begin() as connection:
            assert await connection.scalar(text("SELECT current_user")) == (
                "financeiq_runtime"
            )
            await connection.execute(text(statement), parameters)
    assert getattr(failure.value.orig, "sqlstate", None) == "23503"

    async with accounts_engine.begin() as connection:
        assert (
            await connection.scalar(
                text(
                    f"SELECT revocation_action_id FROM {table} WHERE id=:confirmation"
                ),
                parameters,
            )
            is None
        )
        # The same SQL succeeds for the owner: ACL/CHECK/guards did not cause
        # the negative result, and a fresh transaction works after rollback.
        if column == "revocation_action_id":
            action = await add_action(
                connection, owner, account, action_type.replace("confirm", "revoke")
            )
        await connection.execute(
            text(statement), parameters | {"account": account, "action": action}
        )


@pytest.mark.parametrize("table", ["balance_confirmations", "history_confirmations"])
async def test_unique_active_confirmation_and_preserved_revocation(
    accounts_engine: AsyncEngine,
    accounts_migrator: AsyncEngine,
    seeded: dict[Table, UUID],
    table: ConfirmationTable,
) -> None:
    async with accounts_migrator.begin() as connection:
        with pytest.raises(IntegrityError):
            await copy_row(connection, table, seeded[table], {})
    async with accounts_engine.begin() as connection:
        owner = await connection.scalar(
            text("SELECT user_id FROM accounts WHERE id=:id"),
            {"id": seeded["accounts"]},
        )
        assert isinstance(owner, UUID)
        revoke = await add_action(
            connection,
            owner,
            seeded["accounts"],
            "revoke_balance" if table == "balance_confirmations" else "revoke_history",
        )
        await connection.execute(
            text(f"UPDATE {table} SET revocation_action_id=:revoke WHERE id=:id"),
            {"revoke": revoke, "id": seeded[table]},
        )
        await connection.execute(
            text(
                f"UPDATE {table} SET revocation_action_id=revocation_action_id WHERE id=:id"
            ),
            {"id": seeded[table]},
        )
    async with accounts_migrator.begin() as connection:
        new_id = await copy_row(
            connection, table, seeded[table], {"revocation_action_id": "NULL"}
        )
    async with accounts_engine.connect() as connection:
        assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 2
        assert (
            await connection.scalar(
                text(f"SELECT revocation_action_id FROM {table} WHERE id=:id"),
                {"id": seeded[table]},
            )
            == revoke
        )
        assert (
            await connection.scalar(
                text(f"SELECT revocation_action_id FROM {table} WHERE id=:id"),
                {"id": new_id},
            )
            is None
        )
    for assignment in ("NULL", f"'{seeded['financial_actions']}'"):
        async with accounts_engine.begin() as connection:
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text(
                        f"UPDATE {table} SET revocation_action_id={assignment} WHERE id=:id"
                    ),
                    {"id": seeded[table]},
                )


async def test_overlapping_history_and_empty_day_are_valid(
    accounts_migrator: AsyncEngine, seeded: dict[Table, UUID]
) -> None:
    async with accounts_migrator.begin() as connection:
        await copy_row(
            connection,
            "history_confirmations",
            seeded["history_confirmations"],
            {"start_date": "'2026-10-02'"},
        )
        await copy_row(
            connection,
            "history_confirmations",
            seeded["history_confirmations"],
            {"start_date": "'2026-10-03'"},
        )
        assert (
            await connection.scalar(text("SELECT count(*) FROM history_confirmations"))
            == 3
        )


@pytest.mark.parametrize(
    ("table", "assignment"),
    [
        ("balance_confirmations", "balance=1"),
        ("balance_confirmations", "balance_date=balance_date + 1"),
        ("balance_confirmations", "confirmation_action_id=:action"),
        ("balance_confirmations", "id=:new_id"),
        ("balance_confirmations", "created_at=created_at + interval '1 second'"),
        ("history_confirmations", "observed_facts_version=1"),
        (
            "history_confirmations",
            "observed_facts_digest=decode(repeat('cd',32),'hex')",
        ),
        ("history_confirmations", "start_date=start_date + 1"),
        ("history_confirmations", "end_date=end_date + 1"),
        ("history_confirmations", "account_id=:new_id"),
        ("history_confirmations", "user_id=:new_id"),
    ],
)
async def test_guards_reject_rewriting_source_fields(
    accounts_migrator: AsyncEngine,
    seeded: dict[Table, UUID],
    table: ConfirmationTable,
    assignment: str,
) -> None:
    # Migrator bypasses column ACL, proving the UPDATE guard itself is effective.
    async with accounts_migrator.begin() as connection:
        with pytest.raises(IntegrityError) as caught:
            await connection.execute(
                text(f"UPDATE {table} SET {assignment} WHERE id=:id"),
                {
                    "id": seeded[table],
                    "new_id": uuid4(),
                    "action": seeded["financial_actions"],
                },
            )
        assert getattr(caught.value.orig, "sqlstate", None) == "23514"


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE accounts SET id=id",
        "UPDATE accounts SET user_id=user_id",
        "UPDATE accounts SET created_at=created_at",
        "UPDATE accounts SET currency_code=currency_code",
        "UPDATE accounts SET facts_version=facts_version",
        "INSERT INTO accounts(id, user_id, name, currency_code, facts_version) SELECT :id, user_id, name, currency_code, 1 FROM accounts",
        "INSERT INTO balance_confirmations SELECT * FROM balance_confirmations",
        "INSERT INTO history_confirmations SELECT * FROM history_confirmations",
        "UPDATE balance_confirmations SET balance=balance",
        "UPDATE history_confirmations SET start_date=start_date",
        "UPDATE financial_actions SET details=details",
        "INSERT INTO financial_actions SELECT * FROM financial_actions",
        "DELETE FROM accounts",
        "DELETE FROM balance_confirmations",
        "DELETE FROM history_confirmations",
        "DELETE FROM financial_actions",
        "TRUNCATE accounts CASCADE",
        "TRUNCATE financial_actions CASCADE",
        "TRUNCATE balance_confirmations",
        "TRUNCATE history_confirmations",
        "ALTER TABLE balance_confirmations DISABLE TRIGGER ALL",
        "DROP TABLE history_confirmations",
    ],
)
async def test_runtime_permissions(
    accounts_engine: AsyncEngine, seeded: dict[Table, UUID], sql: str
) -> None:
    async with accounts_engine.begin() as connection:
        with pytest.raises(DBAPIError) as caught:
            await connection.execute(text(sql), {"id": uuid4()})
        assert getattr(caught.value.orig, "sqlstate", None) == "42501"


async def test_runtime_can_rename_archive_restore_and_record_archived_confirmations(
    accounts_engine: AsyncEngine, seeded: dict[Table, UUID]
) -> None:
    async with accounts_engine.begin() as connection:
        owner = await connection.scalar(
            text("SELECT user_id FROM accounts WHERE id=:id"),
            {"id": seeded["accounts"]},
        )
        assert isinstance(owner, UUID)
        await add_action(connection, owner, seeded["accounts"], "archive_account")
        await connection.execute(
            text(
                "UPDATE accounts SET name='Renamed', archived_at=clock_timestamp() WHERE id=:id"
            ),
            {"id": seeded["accounts"]},
        )
        assert await connection.scalar(
            text(
                "SELECT archived_at IS NOT NULL AND name='Renamed' FROM accounts WHERE id=:id"
            ),
            {"id": seeded["accounts"]},
        )
        action = await add_action(
            connection, owner, seeded["accounts"], "confirm_balance"
        )
        await connection.execute(
            text(
                "INSERT INTO balance_confirmations(id,user_id,account_id,balance_date,balance,confirmation_action_id) VALUES (:id,:owner,:account,'2026-09-01',-1,:action)"
            ),
            {
                "id": uuid4(),
                "owner": owner,
                "account": seeded["accounts"],
                "action": action,
            },
        )
        await add_action(connection, owner, seeded["accounts"], "restore_account")
        await connection.execute(
            text("UPDATE accounts SET archived_at=NULL WHERE id=:id"),
            {"id": seeded["accounts"]},
        )
        assert (
            await connection.scalar(
                text("SELECT archived_at FROM accounts WHERE id=:id"),
                {"id": seeded["accounts"]},
            )
            is None
        )


async def test_action_clock_is_insertion_time(accounts_engine: AsyncEngine) -> None:
    async with accounts_engine.begin() as connection:
        owner = await add_owner(connection)
        account = await add_account(connection, owner)
        marker = await connection.scalar(text("SELECT clock_timestamp()"))
        action = await add_action(connection, owner, account)
        created = await connection.scalar(
            text("SELECT created_at FROM financial_actions WHERE id=:id"),
            {"id": action},
        )
        assert created >= marker
        assert created > await connection.scalar(text("SELECT now()"))


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM accounts WHERE id=:id",
        "UPDATE accounts SET id=:new_id WHERE id=:id",
        "DELETE FROM financial_actions WHERE id=(SELECT confirmation_action_id FROM balance_confirmations LIMIT 1)",
        "UPDATE financial_actions SET id=:new_id WHERE id=(SELECT confirmation_action_id FROM history_confirmations LIMIT 1)",
        "DELETE FROM users",
    ],
)
async def test_referenced_resources_are_restricted(
    accounts_migrator: AsyncEngine, seeded: dict[Table, UUID], sql: str
) -> None:
    async with accounts_migrator.begin() as connection:
        with pytest.raises(IntegrityError) as caught:
            await connection.execute(
                text(sql), {"id": seeded["accounts"], "new_id": uuid4()}
            )
        assert getattr(caught.value.orig, "sqlstate", None) == "23001"


async def test_revocation_and_replacement_roll_back_together(
    accounts_engine: AsyncEngine, seeded: dict[Table, UUID]
) -> None:
    with pytest.raises(IntegrityError):
        async with accounts_engine.begin() as connection:
            owner = await connection.scalar(
                text("SELECT user_id FROM accounts WHERE id=:id"),
                {"id": seeded["accounts"]},
            )
            assert isinstance(owner, UUID)
            action = await add_action(
                connection, owner, seeded["accounts"], "revoke_balance"
            )
            await connection.execute(
                text("UPDATE balance_confirmations SET revocation_action_id=:action"),
                {"action": action},
            )
            await connection.execute(
                text(
                    "INSERT INTO balance_confirmations(id,user_id,account_id,balance_date,balance,confirmation_action_id) VALUES (:id,:owner,:account,'2026-10-01',NULL,:action)"
                ),
                {
                    "id": uuid4(),
                    "owner": owner,
                    "account": seeded["accounts"],
                    "action": action,
                },
            )
    async with accounts_engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT count(*) FROM balance_confirmations"))
            == 1
        )
        assert (
            await connection.scalar(
                text("SELECT revocation_action_id FROM balance_confirmations")
            )
            is None
        )
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM financial_actions WHERE action_type='revoke_balance'"
                )
            )
            == 0
        )


@pytest.mark.parametrize("table", ["balance_confirmations", "history_confirmations"])
async def test_active_uniqueness_serializes_competing_transactions(
    accounts_engine: AsyncEngine,
    accounts_migrator: AsyncEngine,
    seeded: dict[Table, UUID],
    table: ConfirmationTable,
) -> None:
    async with accounts_migrator.begin() as connection:
        await connection.execute(
            text(f"UPDATE {table} SET revocation_action_id=:action"),
            {"action": seeded["financial_actions"]},
        )
    # Both sessions use migrator only to clone a complete row; uniqueness is a DB invariant.
    async with (
        accounts_migrator.connect() as first,
        accounts_migrator.connect() as second,
    ):
        first_tx = await first.begin()
        await copy_row(first, table, seeded[table], {"revocation_action_id": "NULL"})
        second_tx = await second.begin()
        pid = await second.scalar(text("SELECT pg_backend_pid()"))
        contender = asyncio.create_task(
            copy_row(second, table, seeded[table], {"revocation_action_id": "NULL"})
        )
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
            await second_tx.rollback()
    async with accounts_engine.connect() as connection:
        assert (
            await connection.scalar(
                text(f"SELECT count(*) FROM {table} WHERE revocation_action_id IS NULL")
            )
            == 1
        )
