"""Owner serialization and fresh reads across independent runtime sessions."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from financeiq.db.locks import lock_financial_owner, lock_financial_rows
from financeiq.db.session import transaction
from tests.support.financial import (
    accept_book,
    archive_account,
    book,
    cancel,
    classify,
    confirmation,
    linked_book,
    snapshot,
    transfer,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

type Command = Callable[[AsyncConnection], Awaitable[None]]


async def wait_for_block(
    observer: AsyncConnection,
    waiter: int,
    blocker: int,
    task: asyncio.Task[str | None],
) -> None:
    async with asyncio.timeout(5):
        while not await observer.scalar(
            text("SELECT :blocker = ANY(pg_blocking_pids(:waiter))"),
            {"waiter": waiter, "blocker": blocker},
        ):
            assert not task.done(), f"Writer finished before waiting: {task.result()}"
            await asyncio.sleep(0.01)


async def race(
    engine: AsyncEngine,
    first: Command,
    second: Command,
    *,
    before_release: Command | None = None,
) -> str | None:
    async with engine.connect() as holder, engine.connect() as waiter:
        async with waiter.begin():
            waiter_pid = await backend_pid(waiter)

        task = None
        try:
            async with holder.begin():
                holder_pid = await backend_pid(holder)
                await first(holder)
                task = asyncio.create_task(run_command(waiter, second))
                await wait_for_block(holder, waiter_pid, holder_pid, task)
                if before_release is not None:
                    await before_release(holder)
            return await asyncio.wait_for(task, timeout=5)
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def backend_pid(connection: AsyncConnection) -> int:
    pid = await connection.scalar(text("SELECT pg_backend_pid()"))
    assert isinstance(pid, int)
    return pid


async def run_command(connection: AsyncConnection, command: Command) -> str | None:
    try:
        async with connection.begin():
            await command(connection)
    except DBAPIError as error:
        state = getattr(error.orig, "sqlstate", None)
        if not isinstance(state, str):
            raise
        return state
    return None


async def take_root(connection: AsyncConnection, owner: UUID) -> None:
    # Run the application entry point on the connection owned by the test.
    async with AsyncSession(
        bind=connection, join_transaction_mode="rollback_only"
    ) as session:
        async with session.begin():
            await lock_financial_owner(session, owner)


async def category(connection: AsyncConnection, owner: UUID) -> UUID:
    identity = uuid4()
    await connection.execute(
        text(
            "INSERT INTO categories(id,user_id,name,name_key,kind) "
            "VALUES (:id,:owner,'Synthetic','synthetic','expense')"
        ),
        {"id": identity, "owner": owner},
    )
    return identity


@pytest.mark.parametrize("archive_first", [True, False])
async def test_category_archive_and_direct_rule_insert(
    financial_engine: AsyncEngine, archive_first: bool
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)
        identity = await category(connection, value.owner)
    rule_id = uuid4()

    async def archive(connection: AsyncConnection) -> None:
        await connection.execute(
            text("UPDATE categories SET archived_at=clock_timestamp() WHERE id=:id"),
            {"id": identity},
        )

    async def add_rule(connection: AsyncConnection) -> None:
        await connection.execute(
            text(
                "INSERT INTO categorization_rules(id,user_id,category_id,pattern,priority) "
                "VALUES (:id,:owner,:category,'x',10)"
            ),
            {"id": rule_id, "owner": value.owner, "category": identity},
        )

    if archive_first:
        assert await race(financial_engine, archive, add_rule) == "23514"
    else:
        assert await race(financial_engine, add_rule, archive) is None

    async with financial_engine.connect() as connection:
        assert await connection.scalar(
            text("SELECT archived_at IS NOT NULL FROM categories WHERE id=:id"),
            {"id": identity},
        )
        rules = await connection.scalars(
            text("SELECT is_active FROM categorization_rules WHERE id=:id"),
            {"id": rule_id},
        )
        assert rules.all() == ([] if archive_first else [False])


async def test_two_partial_refunds_cannot_exceed_the_original(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection, ("-100.00", "60.00", "60.00"))
    original, first, second = value.facts
    actions: list[UUID] = []

    async def refund(connection: AsyncConnection, fact: UUID) -> None:
        identity = await classify(
            connection,
            value,
            fact,
            kind="link_refund",
            changes={"kind": "refund", "refund_of_transaction_id": original},
        )
        actions.append(identity)

    async def first_refund(connection: AsyncConnection) -> None:
        await refund(connection, first)
        await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    async def second_refund(connection: AsyncConnection) -> None:
        await refund(connection, second)

    assert await race(financial_engine, first_refund, second_refund) == "23514"
    assert len(actions) == 2
    async with financial_engine.connect() as connection:
        refunds = await connection.execute(
            text(
                "SELECT id,amount FROM transactions WHERE refund_of_transaction_id=:id"
            ),
            {"id": original},
        )
        assert [tuple(row) for row in refunds] == [(first, Decimal("60.00"))]
        assert (
            await connection.scalar(
                text("SELECT revision FROM transactions WHERE id=:id"), {"id": second}
            )
            == 1
        )
        assert not await connection.scalar(
            text("SELECT EXISTS(SELECT 1 FROM financial_actions WHERE id=:id)"),
            {"id": actions[1]},
        )


async def test_concurrent_confirm_reads_saved_result_after_waiting(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        stage = await book(connection, accept=False)

    async def confirm(connection: AsyncConnection) -> None:
        await take_root(connection, stage.owner)
        await accept_book(connection, stage)

    async def repeat(connection: AsyncConnection) -> None:
        await take_root(connection, stage.owner)
        saved = await connection.execute(
            text(
                "SELECT p.status,p.accepted_action_id,a.plan_digest "
                "FROM import_attempts p JOIN financial_actions a "
                "ON a.user_id=p.user_id AND a.id=p.accepted_action_id "
                "WHERE p.user_id=:owner AND p.id=:id"
            ),
            {"owner": stage.owner, "id": stage.attempt},
        )
        assert saved.one() == ("accepted", stage.action, b"e" * 32)

    assert await race(financial_engine, confirm, repeat) is None
    async with financial_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM transactions")) == 1
        assert (
            await connection.scalar(text("SELECT count(*) FROM financial_actions")) == 1
        )


async def test_confirm_rejects_stale_classification_after_waiting(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)
        fact = value.facts[0]
        stale = await snapshot(connection, fact)

    async def reclassify(connection: AsyncConnection) -> None:
        await classify(connection, value, fact)

    async def confirm(connection: AsyncConnection) -> None:
        await take_root(connection, value.owner)
        await linked_book(connection, value, saved_classification=stale)

    assert await race(financial_engine, reclassify, confirm) == "23514"
    async with financial_engine.begin() as connection:
        current = await snapshot(connection, fact)
        assert current["kind"] == "unclassified"
        assert (
            await connection.scalar(text("SELECT count(*) FROM import_attempts")) == 1
        )
        linked = await linked_book(connection, value)
        saved = await connection.scalar(
            text(
                "SELECT details->'result'->'classifications'->0->'state' "
                "FROM financial_actions WHERE id=:id"
            ),
            {"id": linked.action},
        )
        assert saved == current


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_cancellation_and_external_link_cannot_both_commit(
    financial_engine: AsyncEngine, cancel_first: bool
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)

    async def exclude(connection: AsyncConnection) -> None:
        await cancel(connection, value)

    async def link(connection: AsyncConnection) -> None:
        await linked_book(connection, value)

    first, second = (exclude, link) if cancel_first else (link, exclude)
    assert await race(financial_engine, first, second) == "23514"
    async with financial_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT cancelled_by_id IS NOT NULL FROM transactions")
            )
            == cancel_first
        )
        assert await connection.scalar(
            text("SELECT count(*) FROM import_movements WHERE decision='link'")
        ) == int(not cancel_first)
        assert await connection.scalar(
            text("SELECT count(*) FROM import_cancellations")
        ) == int(cancel_first)
        assert (
            await connection.scalar(text("SELECT count(*) FROM financial_actions")) == 2
        )


@pytest.mark.parametrize("archive_first", [True, False])
async def test_account_archive_and_confirm(
    financial_engine: AsyncEngine, archive_first: bool
) -> None:
    async with financial_engine.begin() as connection:
        stage = await book(connection, accept=False)

    async def archive(connection: AsyncConnection) -> None:
        await archive_account(connection, stage)

    async def confirm(connection: AsyncConnection) -> None:
        await accept_book(connection, stage)

    first, second = (archive, confirm) if archive_first else (confirm, archive)
    assert await race(financial_engine, first, second) == (
        "23514" if archive_first else None
    )
    async with financial_engine.connect() as connection:
        assert await connection.scalar(
            text("SELECT archived_at IS NOT NULL FROM accounts")
        )
        assert await connection.scalar(text("SELECT status FROM import_attempts")) == (
            "processing" if archive_first else "accepted"
        )
        assert await connection.scalar(
            text("SELECT count(*) FROM transactions")
        ) == int(not archive_first)
        assert await connection.scalar(
            text("SELECT count(*) FROM financial_actions")
        ) == (1 if archive_first else 2)


@pytest.mark.parametrize("cleanup_first", [False, True])
async def test_staging_cleanup_and_confirm(
    financial_engine: AsyncEngine, cleanup_first: bool
) -> None:
    async with financial_engine.begin() as connection:
        stage = await book(connection, accept=False)

    async def cleanup(connection: AsyncConnection) -> None:
        parameters = {"attempt": stage.attempt}
        await connection.execute(
            text("UPDATE import_attempts SET status='abandoned' WHERE id=:attempt"),
            parameters,
        )
        for table in ("import_movements", "import_rows", "import_account_mappings"):
            await connection.execute(
                text(f"DELETE FROM {table} WHERE attempt_id=:attempt"), parameters
            )
        await connection.execute(
            text(
                "UPDATE import_attempts SET content_present=false, "
                "content_purged_at=clock_timestamp() WHERE id=:attempt"
            ),
            parameters,
        )

    async def confirm(connection: AsyncConnection) -> None:
        await accept_book(connection, stage)

    first, second = (cleanup, confirm) if cleanup_first else (confirm, cleanup)
    assert await race(financial_engine, first, second) == "23514"
    async with financial_engine.connect() as connection:
        stored = await connection.execute(
            text("SELECT status,content_present FROM import_attempts")
        )
        assert stored.one() == (
            ("abandoned", False) if cleanup_first else ("accepted", True)
        )
        for table in (
            "import_movements",
            "import_rows",
            "import_account_mappings",
            "transactions",
            "financial_actions",
        ):
            assert await connection.scalar(
                text(f"SELECT count(*) FROM {table}")
            ) == int(not cleanup_first)


@pytest.mark.parametrize("exclude_fact", [False, True])
@pytest.mark.parametrize("confirm_first", [False, True])
async def test_completeness_and_change_to_the_reviewed_set(
    financial_engine: AsyncEngine, confirm_first: bool, exclude_fact: bool
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection, accept=exclude_fact)

    async def reviewed_facts(connection: AsyncConnection) -> tuple[UUID, ...]:
        facts = await connection.scalars(
            text(
                "SELECT id FROM transactions WHERE user_id=:owner AND account_id=:account "
                "AND occurred_on=:day AND cancelled_by_id IS NULL ORDER BY id"
            ),
            {"owner": value.owner, "account": value.account, "day": date(2026, 10, 1)},
        )
        return tuple(facts)

    async with financial_engine.connect() as connection:
        preview = await reviewed_facts(connection)

    async def confirm(connection: AsyncConnection) -> None:
        await take_root(connection, value.owner)
        if await reviewed_facts(connection) == preview:
            await confirmation(connection, value)

    async def change(connection: AsyncConnection) -> None:
        if exclude_fact:
            await cancel(connection, value)
        else:
            await accept_book(connection, value)

    first, second = (confirm, change) if confirm_first else (change, confirm)
    assert await race(financial_engine, first, second) is None
    async with financial_engine.connect() as connection:
        assert await reviewed_facts(connection) == (() if exclude_fact else value.facts)
        active = await connection.scalars(
            text("SELECT revocation_action_id IS NULL FROM history_confirmations")
        )
        assert active.all() == ([False] if confirm_first else [])


async def test_root_is_scoped_and_compatible_with_reads(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        owner = await book(connection)
        other = await book(connection)

    async with financial_engine.begin() as holder:
        await take_root(holder, owner.owner)
        async with asyncio.timeout(5):
            async with financial_engine.begin() as writer:
                await writer.execute(
                    text("UPDATE accounts SET name='Updated' WHERE id=:id"),
                    {"id": other.account},
                )
                assert (
                    await writer.scalar(
                        text("SELECT name FROM accounts WHERE id=:id"),
                        {"id": owner.account},
                    )
                    == "Synthetic"
                )
            async with financial_engine.begin() as reader:
                await reader.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                )
                assert (
                    await reader.scalar(text("SELECT count(*) FROM transactions")) == 2
                )
            async with financial_engine.begin() as auth:
                await auth.execute(
                    text(
                        "INSERT INTO auth_sessions(id,user_id,token_hash,csrf_token_hash,expires_at) "
                        "VALUES (:id,:owner,:token,:csrf,now()+interval '1 hour')"
                    ),
                    {
                        "id": uuid4(),
                        "owner": owner.owner,
                        "token": b"t" * 32,
                        "csrf": b"c" * 32,
                    },
                )


@pytest.mark.parametrize("level", ["REPEATABLE READ", "SERIALIZABLE"])
@pytest.mark.parametrize("explicit", [False, True])
async def test_financial_writes_require_read_committed(
    financial_engine: AsyncEngine, level: str, explicit: bool
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)

    async def write(connection: AsyncConnection) -> None:
        await connection.execute(text(f"SET TRANSACTION ISOLATION LEVEL {level}"))
        await connection.scalar(text("SELECT count(*) FROM accounts"))
        if explicit:
            await take_root(connection, value.owner)
        else:
            await connection.execute(
                text("UPDATE accounts SET name='Updated' WHERE id=:id"),
                {"id": value.account},
            )

    async with financial_engine.connect() as connection:
        assert await run_command(connection, write) == "0A000"


async def test_scoped_helper_locks_all_target_groups(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection, ("-100.00", "100.00"))
        category_id = await category(connection, value.owner)
        rule = uuid4()
        await connection.execute(
            text(
                "INSERT INTO categorization_rules(id,user_id,category_id,pattern,priority) "
                "VALUES (:id,:owner,:category,'x',10)"
            ),
            {"id": rule, "owner": value.owner, "category": category_id},
        )
        link = await transfer(connection, value, value.facts[0])
        other = await book(connection)

    async with AsyncSession(financial_engine, autobegin=False) as session:
        with pytest.raises(RuntimeError, match="active transaction"):
            await lock_financial_owner(session, value.owner)
        async with transaction(session):
            await lock_financial_owner(session, value.owner)
            await lock_financial_rows(
                session,
                value.owner,
                account_ids=[value.account],
                batch_ids=[value.batch, value.batch],
                attempt_ids=[value.attempt],
                category_ids=[category_id],
                rule_ids=[rule],
                transaction_ids=tuple(reversed(value.facts)),
                transfer_link_ids=[link],
            )
        with pytest.raises(LookupError, match="target"):
            async with transaction(session):
                await lock_financial_rows(session, value.owner, batch_ids=[other.batch])
        with pytest.raises(LookupError, match="target"):
            async with transaction(session):
                await lock_financial_rows(
                    session, value.owner, transaction_ids=[uuid4()]
                )


async def test_rows_in_a_group_are_locked_in_uuid_order(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)
        other = await book(connection, owner=value.owner)
    lower, higher = sorted([value.account, other.account])

    async def hold_higher(connection: AsyncConnection) -> None:
        await connection.execute(
            text("SELECT id FROM accounts WHERE id=:id FOR NO KEY UPDATE"),
            {"id": higher},
        )

    async def acquire_both(connection: AsyncConnection) -> None:
        async with AsyncSession(
            bind=connection, join_transaction_mode="rollback_only"
        ) as session:
            async with session.begin():
                await lock_financial_rows(
                    session, value.owner, account_ids=[higher, lower]
                )

    async def check_lower_already_locked(connection: AsyncConnection) -> None:
        with pytest.raises(DBAPIError) as failure:
            async with connection.begin_nested():
                await connection.execute(
                    text(
                        "SELECT id FROM accounts WHERE id=:id FOR NO KEY UPDATE NOWAIT"
                    ),
                    {"id": lower},
                )
        assert getattr(failure.value.orig, "sqlstate", None) == "55P03"

    assert (
        await race(
            financial_engine,
            hold_higher,
            acquire_both,
            before_release=check_lower_already_locked,
        )
        is None
    )


async def test_action_clock_is_sampled_after_waiting_for_the_owner(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)
    samples: list[datetime] = []
    actions: list[UUID] = []

    async def hold_root(connection: AsyncConnection) -> None:
        await take_root(connection, value.owner)

    async def write_action(connection: AsyncConnection) -> None:
        actions.append(await classify(connection, value, value.facts[0]))

    async def sample_clock(connection: AsyncConnection) -> None:
        sample = await connection.scalar(text("SELECT clock_timestamp()"))
        assert isinstance(sample, datetime)
        samples.append(sample)

    assert (
        await race(
            financial_engine, hold_root, write_action, before_release=sample_clock
        )
        is None
    )
    async with financial_engine.connect() as connection:
        created = await connection.scalar(
            text("SELECT created_at FROM financial_actions WHERE id=:id"),
            {"id": actions[0]},
        )
        assert isinstance(created, datetime)
        assert created >= samples[0]


@pytest.mark.parametrize(
    "statement",
    [
        "DO $$ BEGIN PERFORM public.guard_financial_owner_lock(); END $$",
        "SELECT id FROM import_batches FOR NO KEY UPDATE",
        "ALTER TABLE accounts DISABLE TRIGGER a_financial_owner_lock",
        "TRUNCATE import_rows",
    ],
)
async def test_runtime_cannot_bypass_guard_or_modify_batch_privileges(
    financial_engine: AsyncEngine, statement: str
) -> None:
    async def execute(connection: AsyncConnection) -> None:
        await connection.execute(text(statement))

    async with financial_engine.connect() as connection:
        assert await run_command(connection, execute) == "42501"


async def test_deadlock_rolls_back_a_whole_command(
    financial_engine: AsyncEngine,
) -> None:
    async with financial_engine.begin() as connection:
        value = await book(connection)

    async def rename(connection: AsyncConnection, name: str) -> None:
        await connection.execute(
            text("UPDATE accounts SET name=:name WHERE id=:id"),
            {"name": name, "id": value.account},
        )

    async def direct_writer(connection: AsyncConnection) -> None:
        # PostgreSQL has locked the target row by the time its BEFORE UPDATE
        # guard waits on the root. The service writer then requests that row.
        await rename(connection, "Direct")

    async with (
        financial_engine.connect() as service,
        financial_engine.connect() as direct,
    ):
        async with direct.begin():
            direct_pid = await backend_pid(direct)
        task = None
        service_state = None
        direct_state = None
        try:
            async with service.begin():
                service_pid = await backend_pid(service)
                await take_root(service, value.owner)
                await category(service, value.owner)
                task = asyncio.create_task(run_command(direct, direct_writer))
                await wait_for_block(service, direct_pid, service_pid, task)
                async with asyncio.timeout(5):
                    await rename(service, "Service")
        except DBAPIError as error:
            service_state = getattr(error.orig, "sqlstate", None)
            assert service_state == "40P01"
        finally:
            if task is not None:
                direct_state = await asyncio.wait_for(task, timeout=5)
        assert (service_state, direct_state) in ((None, "40P01"), ("40P01", None))

    async with financial_engine.begin() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM categories")) == int(
            service_state is None
        )
        assert await connection.scalar(text("SELECT name FROM accounts")) == (
            "Service" if service_state is None else "Direct"
        )
        await rename(connection, "Next command")
