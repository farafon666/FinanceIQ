"""Real runtime transactions; technical probes do not implement domain guards."""

import asyncio
import logging
import os
import traceback
from contextlib import AsyncExitStack
from decimal import Decimal
from uuid import uuid4

import asyncpg
import httpx
import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financeiq.api.dependencies import DatabaseSession
from financeiq.db.errors import DatabaseError
from financeiq.db.session import session_scope, transaction
from financeiq.main import create_app
from tests.support.postgres import TestPostgres
from tests.support.probes import ProbeEntry, ProbeParent, build_entry, build_parent

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]
SENTINEL = "private-synthetic-note"


async def add_pair(
    session: AsyncSession, amount: Decimal = Decimal("12.34")
) -> ProbeEntry:
    parent = build_parent()
    entry = build_entry(parent.id, amount=amount, note=SENTINEL)
    session.add_all([parent, entry])
    await session.flush()
    return entry


async def counts(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    async with session_scope(factory) as session, transaction(session):
        parents = await session.scalar(select(func.count()).select_from(ProbeParent))
        entries = await session.scalar(select(func.count()).select_from(ProbeEntry))
        assert isinstance(parents, int) and isinstance(entries, int)
        return parents, entries


async def test_connects_as_runtime_with_expected_isolation(
    runtime_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(runtime_sessions) as session, transaction(session):
        row = (
            await session.execute(
                text("SELECT current_user, current_setting('transaction_isolation'), 1")
            )
        ).one()
        assert tuple(row) == ("financeiq_runtime", "read committed", 1)


async def test_commit_is_visible_only_after_completion(
    runtime_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(runtime_sessions) as session:
        async with transaction(session):
            entry = await add_pair(session)
            assert await counts(runtime_sessions) == (0, 0)
        assert not session.in_transaction()
        # expire_on_commit=False keeps the committed value available.
        assert entry.amount == Decimal("12.34")
    assert await counts(runtime_sessions) == (1, 1)


async def test_concurrent_scopes_use_independent_connections(
    runtime_sessions: async_sessionmaker[AsyncSession],
) -> None:
    ready, release = asyncio.Event(), asyncio.Event()

    async def writer() -> int:
        async with session_scope(runtime_sessions) as session, transaction(session):
            await add_pair(session)
            pid = await session.scalar(text("SELECT pg_backend_pid()"))
            assert isinstance(pid, int)
            ready.set()
            await release.wait()
            return pid

    async with asyncio.timeout(10):
        task = asyncio.create_task(writer())
        try:
            await ready.wait()
            async with session_scope(runtime_sessions) as reader, transaction(reader):
                reader_pid = await reader.scalar(text("SELECT pg_backend_pid()"))
                assert (
                    await reader.scalar(select(func.count()).select_from(ProbeEntry))
                    == 0
                )
            release.set()
            assert await task != reader_pid
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert await counts(runtime_sessions) == (1, 1)


async def test_business_failure_rolls_back_all_writes(
    runtime_sessions: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(ValueError, match="business failure"):
        async with session_scope(runtime_sessions) as session, transaction(session):
            await add_pair(session)
            raise ValueError("business failure")
    assert await counts(runtime_sessions) == (0, 0)


async def test_closing_unfinished_session_does_not_commit(
    runtime_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(runtime_sessions) as session:
        await session.begin()
        await add_pair(session)
    assert await counts(runtime_sessions) == (0, 0)


async def test_deferred_fk_can_be_repaired_after_flush(
    runtime_sessions: async_sessionmaker[AsyncSession],
) -> None:
    parent = build_parent()
    async with session_scope(runtime_sessions) as session, transaction(session):
        session.add(build_entry(parent.id))
        await session.flush()  # Parent does not exist yet.
        session.add(parent)
    assert await counts(runtime_sessions) == (1, 1)


@pytest.mark.parametrize("failure_at", ["flush", "commit"])
async def test_failed_transaction_is_atomic_safe_and_session_recovers(
    runtime_sessions: async_sessionmaker[AsyncSession],
    runtime_postgres: TestPostgres,
    caplog: pytest.LogCaptureFixture,
    failure_at: str,
) -> None:
    flushed = False
    async with session_scope(runtime_sessions) as session:
        with caplog.at_level(logging.DEBUG), pytest.raises(DatabaseError) as caught:
            async with transaction(session):
                entry = await add_pair(session)
                if failure_at == "flush":
                    # Immediate PK violation after another write already succeeded.
                    await session.execute(
                        insert(ProbeParent).values(id=entry.parent_id)
                    )
                else:
                    session.add(build_entry(uuid4(), note=SENTINEL))
                await session.flush()
                flushed = True
        assert flushed is (failure_at == "commit")
        assert not session.in_transaction()
        rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
        runtime_postgres.assert_safe(rendered)
        for private in (
            SENTINEL,
            "test_runtime_entry",
            "test_runtime_parent",
            "INSERT INTO",
            "IntegrityError",
        ):
            assert private not in rendered
        assert str(caught.value) == "Database operation failed."
        assert await counts(runtime_sessions) == (0, 0)
        async with transaction(session):
            await add_pair(session)
    assert await counts(runtime_sessions) == (1, 1)


@pytest.mark.parametrize(
    "amount",
    [
        Decimal("0.10"),
        Decimal("-0.20"),
        Decimal("1.230"),
        Decimal("-1.230"),
        Decimal("9999999999999999.99"),
        Decimal("-9999999999999999.99"),
    ],
)
async def test_decimal_roundtrip(
    runtime_sessions: async_sessionmaker[AsyncSession],
    amount: Decimal,
) -> None:
    async with session_scope(runtime_sessions) as session, transaction(session):
        entry = await add_pair(session, amount)
    async with session_scope(runtime_sessions) as observer, transaction(observer):
        stored = await observer.scalar(
            select(ProbeEntry.amount).where(ProbeEntry.id == entry.id)
        )
        assert isinstance(stored, Decimal)
        assert stored == amount
        assert stored.as_tuple().exponent == -2


@pytest.mark.parametrize(
    "amounts, expected",
    [
        ([Decimal("0.10"), Decimal("0.20")], Decimal("0.30")),
        ([Decimal("9999999999999999.99")] * 2, Decimal("19999999999999999.98")),
    ],
)
async def test_sum_preserves_decimal_precision_and_aggregate_range(
    runtime_sessions: async_sessionmaker[AsyncSession],
    amounts: list[Decimal],
    expected: Decimal,
) -> None:
    async with session_scope(runtime_sessions) as session, transaction(session):
        for amount in amounts:
            await add_pair(session, amount)
    async with session_scope(runtime_sessions) as observer, transaction(observer):
        result = await observer.scalar(select(func.sum(ProbeEntry.amount)))
        assert isinstance(result, Decimal)
        assert result == sum(amounts, Decimal("0.00")) == expected


@pytest.mark.parametrize(
    "amount", [Decimal("10000000000000000.00"), Decimal("-10000000000000000.00")]
)
async def test_numeric_overflow_rolls_back_command(
    runtime_sessions: async_sessionmaker[AsyncSession],
    amount: Decimal,
) -> None:
    with pytest.raises(DatabaseError):
        async with session_scope(runtime_sessions) as session, transaction(session):
            await add_pair(session)
            await add_pair(session, amount)
    assert await counts(runtime_sessions) == (0, 0)


async def terminate(database: TestPostgres, pid: int) -> None:
    connection = await asyncpg.connect(database.url("postgres"), timeout=5)
    try:
        assert await connection.fetchval("SELECT pg_terminate_backend($1, 5000)", pid)
    finally:
        await connection.close()


@pytest.mark.parametrize("active", [False, True])
async def test_terminated_connection_recovers_without_replaying_writes(
    runtime_sessions: async_sessionmaker[AsyncSession],
    runtime_postgres: TestPostgres,
    active: bool,
) -> None:
    async with session_scope(runtime_sessions) as session:
        if active:
            with pytest.raises(DatabaseError):
                async with transaction(session):
                    await add_pair(session)
                    pid = await session.scalar(text("SELECT pg_backend_pid()"))
                    assert isinstance(pid, int)
                    await terminate(runtime_postgres, pid)
                    await session.execute(text("SELECT 1"))
        else:
            async with transaction(session):
                pid = await session.scalar(text("SELECT pg_backend_pid()"))
                assert isinstance(pid, int)
            await terminate(runtime_postgres, pid)  # Idle pooled connection.
        async with transaction(session):
            new_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            assert new_pid != pid
            assert (
                await session.scalar(select(func.count()).select_from(ProbeEntry)) == 0
            )
            await add_pair(session)
    assert await counts(runtime_sessions) == (1, 1)


@pytest.mark.parametrize("failure_at", ["connect", "commit"])
async def test_http_failure_is_safe_and_next_request_succeeds(
    runtime_postgres: TestPostgres,
    probe_tables: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_at: str,
) -> None:
    for name in list(os.environ):
        if name.upper().startswith("FINANCEIQ_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FINANCEIQ_APP_ENV", "test")
    url = runtime_postgres.url("financeiq_runtime", sqlalchemy=True)
    monkeypatch.setenv("FINANCEIQ_DATABASE_URL", url)
    app = create_app()
    failed_app = app
    if failure_at == "connect":
        bad_url = make_url(url).set(password="wrong-password-sentinel")
        monkeypatch.setenv(
            "FINANCEIQ_DATABASE_URL", bad_url.render_as_string(hide_password=False)
        )
        failed_app = create_app()

    @failed_app.post("/test-failure")
    async def failed_command(session: DatabaseSession) -> dict[str, bool]:
        async with transaction(session):
            await add_pair(session)
            session.add(build_entry(uuid4(), note=SENTINEL))
            await session.flush()
        return {"success": True}

    @app.post("/test-command")
    async def command(session: DatabaseSession) -> dict[str, bool]:
        async with transaction(session):
            await add_pair(session)
        return {"success": True}

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        if failed_app is not app:
            await stack.enter_async_context(
                failed_app.router.lifespan_context(failed_app)
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=failed_app), base_url="http://test"
        ) as client:
            with caplog.at_level(logging.DEBUG):
                response = await client.post("/test-failure")
            assert response.status_code == 500
            assert response.json()["error"]["code"] == "internal_error"
            rendered = response.text + caplog.text
            runtime_postgres.assert_safe(rendered)
            for private in (
                SENTINEL,
                "test_runtime_entry",
                "test_runtime_parent",
                "INSERT INTO",
                "Traceback",
            ):
                assert private not in rendered
            assert "database_error" in caplog.text
        async with (
            session_scope(app.state.session_factory) as session,
            transaction(session),
        ):
            assert (
                await session.scalar(select(func.count()).select_from(ProbeParent)) == 0
            )
            assert (
                await session.scalar(select(func.count()).select_from(ProbeEntry)) == 0
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/test-command")
            assert response.status_code == 200
            assert response.json() == {"success": True}
        assert await counts(app.state.session_factory) == (1, 1)
