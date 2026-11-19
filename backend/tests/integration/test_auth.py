"""Auth persistence and permissions on migrated disposable PostgreSQL only."""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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
    session_scope,
    transaction,
)
from financeiq.modules.auth.domain import normalize_email
from financeiq.modules.auth.models import AuthSession, User
from tests.support.postgres import TestPostgres

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def auth_migrator(runtime_postgres: TestPostgres) -> AsyncIterator[AsyncEngine]:
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
async def auth_engine(
    runtime_postgres: TestPostgres, auth_migrator: AsyncEngine
) -> AsyncIterator[AsyncEngine]:
    runtime = create_engine(
        Settings(
            app_env="test",
            database_url=runtime_postgres.url("financeiq_runtime", sqlalchemy=True),
        )
    )
    try:
        async with auth_migrator.begin() as connection:
            await connection.execute(text("DELETE FROM auth_sessions"))
            await connection.execute(text("DELETE FROM users"))
        yield runtime
    finally:
        await runtime.dispose()
        async with auth_migrator.begin() as connection:
            await connection.execute(text("DELETE FROM auth_sessions"))
            await connection.execute(text("DELETE FROM users"))


async def insert_user(
    connection: AsyncConnection, email: str = "user@example.com"
) -> UUID:
    identity = uuid4()
    await connection.execute(
        text(
            "INSERT INTO users(id, email, password_hash, timezone) "
            "VALUES (:id, :email, 'synthetic-password-hash', 'Europe/Saratov')"
        ),
        {"id": identity, "email": email},
    )
    return identity


async def insert_session(connection: AsyncConnection, user_id: UUID) -> UUID:
    identity = uuid4()
    await connection.execute(
        text(
            "INSERT INTO auth_sessions(id, user_id, token_hash, csrf_token_hash, "
            "expires_at) VALUES (:id, :user_id, :token, :csrf, now() + interval '1 day')"
        ),
        {
            "id": identity,
            "user_id": user_id,
            "token": hashlib.sha256(identity.bytes).digest(),
            "csrf": hashlib.sha256(b"synthetic-csrf" + identity.bytes).digest(),
        },
    )
    return identity


async def test_orm_roundtrip_and_independent_sessions(auth_engine: AsyncEngine) -> None:
    factory = create_session_factory(auth_engine)
    async with session_scope(factory) as session:
        async with transaction(session):
            first = User(
                email=normalize_email(" Alice.Example+Work@EXAMPLE.COM "),
                password_hash="synthetic-password-hash",
                timezone="Europe/Saratov",
            )
            second = User(
                email="second@example.com",
                password_hash="another-synthetic-hash",
                timezone="UTC",
            )
            session.add_all([first, second])
            await session.flush()
            records = [
                AuthSession(
                    user_id=owner.id,
                    token_hash=hashlib.sha256(
                        f"synthetic-token-{index}".encode()
                    ).digest(),
                    csrf_token_hash=hashlib.sha256(
                        f"synthetic-csrf-{index}".encode()
                    ).digest(),
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
                for index, owner in enumerate((first, first, second))
            ]
            session.add_all(records)
            await session.flush()
            identities = [record.id for record in records]
            first_id, second_id = first.id, second.id

    async with session_scope(factory) as session:
        async with transaction(session):
            loaded = await session.get(User, first_id)
            assert loaded is not None
            assert loaded.email == "alice.example+work@example.com"
            assert loaded.password_hash == "synthetic-password-hash"
            assert loaded.timezone == "Europe/Saratov"
            assert loaded.is_active is True
            assert loaded.created_at.utcoffset() == timedelta(0)
            assert len(set(identities)) == 3
            for index, identity in enumerate(identities):
                record = await session.get(AuthSession, identity)
                assert record is not None
                assert record.user_id == (first_id if index < 2 else second_id)
                assert record.token_hash == records[index].token_hash
                assert record.csrf_token_hash == records[index].csrf_token_hash
                assert record.expires_at == records[index].expires_at
                assert record.created_at.tzinfo is not None
                assert record.revoked_at is None


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("email", ""),
        ("email", "User@example.com"),
        ("email", " user@example.com"),
        ("email", "user@example.com\n"),
        ("email", "user\tname@example.com"),
        ("email", "user\x7f@example.com"),
        ("email", "user@пример.рф"),
        ("email", None),
        ("password_hash", ""),
        ("password_hash", None),
        ("timezone", ""),
        ("timezone", None),
        ("is_active", None),
        ("created_at", "infinity"),
        ("created_at", "-infinity"),
    ],
)
async def test_user_constraints(
    auth_engine: AsyncEngine, auth_migrator: AsyncEngine, column: str, value: str | None
) -> None:
    async with auth_engine.begin() as connection:
        identity = await insert_user(connection)
    expression = (
        "CAST(CAST(:value AS text) AS timestamptz)"
        if column == "created_at"
        else ":value"
    )
    async with auth_migrator.begin() as connection:
        with pytest.raises(IntegrityError):
            await connection.execute(
                text(f"UPDATE users SET {column} = {expression} WHERE id = :id"),
                {"id": identity, "value": value},
            )


@pytest.mark.parametrize(
    "assignment",
    [
        "token_hash = NULL",
        "token_hash = decode(repeat('ab', 31), 'hex')",
        "token_hash = decode(repeat('ab', 33), 'hex')",
        "csrf_token_hash = NULL",
        "csrf_token_hash = decode(repeat('ab', 31), 'hex')",
        "csrf_token_hash = decode(repeat('ab', 33), 'hex')",
        "user_id = NULL",
        "user_id = '00000000-0000-0000-0000-000000000000'",
        "expires_at = NULL",
        "expires_at = 'infinity'",
        "expires_at = '-infinity'",
        "expires_at = created_at",
        "revoked_at = 'infinity'",
        "revoked_at = '-infinity'",
        "revoked_at = created_at - interval '1 second'",
        "created_at = 'infinity'",
    ],
)
async def test_session_constraints(
    auth_engine: AsyncEngine, auth_migrator: AsyncEngine, assignment: str
) -> None:
    async with auth_engine.begin() as connection:
        owner = await insert_user(connection)
        identity = await insert_session(connection, owner)
    # Migrator can exercise immutable-column constraints independently of ACL.
    async with auth_migrator.begin() as connection:
        with pytest.raises(IntegrityError):
            await connection.execute(
                text(f"UPDATE auth_sessions SET {assignment} WHERE id = :id"),
                {"id": identity},
            )


async def test_token_hash_is_unique(auth_engine: AsyncEngine) -> None:
    async with auth_engine.begin() as connection:
        owner = await insert_user(connection)
        identity = await insert_session(connection, owner)
    async with auth_engine.begin() as connection:
        with pytest.raises(IntegrityError):
            await connection.execute(
                text(
                    "INSERT INTO auth_sessions(id, user_id, token_hash, csrf_token_hash, "
                    "expires_at) SELECT :new_id, user_id, token_hash, csrf_token_hash, "
                    "expires_at FROM auth_sessions WHERE id = :id"
                ),
                {"new_id": uuid4(), "id": identity},
            )


async def test_email_uniqueness_serializes_concurrent_inserts(
    auth_engine: AsyncEngine,
) -> None:
    async with auth_engine.connect() as first, auth_engine.connect() as second:
        first_tx = await first.begin()
        await insert_user(first, normalize_email(" Shared@Example.COM "))
        second_tx = await second.begin()
        pid = await second.scalar(text("SELECT pg_backend_pid()"))
        contender = asyncio.create_task(
            insert_user(second, normalize_email("shared@example.com"))
        )
        try:
            async with asyncio.timeout(5):
                while True:
                    blockers = await first.scalar(
                        text("SELECT cardinality(pg_blocking_pids(:pid))"), {"pid": pid}
                    )
                    if blockers:
                        break
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
    async with auth_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM users")) == 1


async def test_runtime_updates_profile_and_revokes_an_expired_session(
    auth_engine: AsyncEngine,
) -> None:
    async with auth_engine.begin() as connection:
        owner = await insert_user(connection)
        identity = await insert_session(connection, owner)
        await connection.execute(
            text(
                "UPDATE users SET email='changed@example.com', password_hash='changed', "
                "timezone='UTC', is_active=false WHERE id=:id"
            ),
            {"id": owner},
        )
    # A historical session is valid storage even though it cannot authorize a request.
    async with auth_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO auth_sessions(id, user_id, token_hash, csrf_token_hash, "
                "created_at, expires_at) VALUES (:id, :owner, :token, :csrf, "
                "now() - interval '2 days', now() - interval '1 day')"
            ),
            {"id": uuid4(), "owner": owner, "token": b"a" * 32, "csrf": b"b" * 32},
        )
        await connection.execute(
            text("UPDATE auth_sessions SET revoked_at=now()"),
        )
        await connection.execute(
            text(
                "UPDATE auth_sessions SET expires_at=expires_at + interval '1 hour', "
                "csrf_token_hash=:csrf WHERE id=:id"
            ),
            {"id": identity, "csrf": b"c" * 32},
        )
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM auth_sessions WHERE revoked_at IS NOT NULL")
            )
            == 2
        )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE users SET id=id",
        "UPDATE users SET created_at=created_at",
        "UPDATE auth_sessions SET id=id",
        "UPDATE auth_sessions SET user_id=user_id",
        "UPDATE auth_sessions SET created_at=created_at",
        "UPDATE auth_sessions SET token_hash=token_hash",
        "DELETE FROM users",
        "DELETE FROM auth_sessions",
        "TRUNCATE auth_sessions",
        "TRUNCATE users CASCADE",
        "ALTER TABLE users ADD COLUMN forbidden integer",
        "DROP TABLE auth_sessions",
        "SELECT * FROM alembic_version",
    ],
)
async def test_runtime_cannot_change_identity_or_delete(
    auth_engine: AsyncEngine, sql: str
) -> None:
    async with auth_engine.begin() as connection:
        owner = await insert_user(connection)
        await insert_session(connection, owner)
    async with auth_engine.begin() as connection:
        with pytest.raises(DBAPIError) as caught:
            await connection.execute(text(sql))
        assert getattr(caught.value.orig, "sqlstate", None) == "42501"


async def test_owner_fk_restricts_delete_and_update(
    auth_engine: AsyncEngine, auth_migrator: AsyncEngine
) -> None:
    async with auth_engine.begin() as connection:
        owner = await insert_user(connection)
        await insert_session(connection, owner)
    for statement in (
        "DELETE FROM users WHERE id=:id",
        "UPDATE users SET id=:new_id WHERE id=:id",
    ):
        async with auth_migrator.begin() as connection:
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text(statement), {"id": owner, "new_id": uuid4()}
                )
