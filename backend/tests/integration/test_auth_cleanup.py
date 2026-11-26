"""Verify bounded Auth cleanup and startup scheduling on real PostgreSQL."""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from financeiq.main import create_app
from tests.support.postgres import TestPostgres

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def seed_auth_state(engine: AsyncEngine, count: int) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("""
            WITH owner AS (
                INSERT INTO users (id, email, password_hash, timezone)
                VALUES (gen_random_uuid(), 'cleanup@example.org', 'test-hash', 'UTC')
                RETURNING id
            )
            INSERT INTO auth_sessions
                (id, user_id, token_hash, csrf_token_hash, created_at, expires_at, revoked_at)
            SELECT gen_random_uuid(), owner.id, sha256(convert_to(kind || n, 'UTF8')),
                decode(repeat('ab', 32), 'hex'),
                statement_timestamp() - interval '12 hours',
                statement_timestamp() + CASE WHEN kind = 'expired'
                    THEN interval '-1 hour' ELSE interval '1 hour' END,
                CASE WHEN kind = 'revoked' THEN statement_timestamp() END
            FROM owner CROSS JOIN (VALUES ('expired'), ('revoked'), ('active')) AS states(kind)
            CROSS JOIN LATERAL generate_series(1, CASE WHEN kind = 'active' THEN 1 ELSE :n END) AS g(n)
            """),
            {"n": count},
        )
        await connection.execute(
            text("""
            INSERT INTO auth_rate_limits (scope, key_digest, window_start, attempt_count)
            SELECT 'login_ip', sha256(convert_to('window' || n, 'UTF8')),
                statement_timestamp() - CASE WHEN n = 0
                    THEN interval '5 minutes' ELSE interval '15 minutes' END, 1
            FROM generate_series(0, :n) AS g(n)
            """),
            {"n": count},
        )


async def auth_counts(engine: AsyncEngine) -> tuple[int, ...]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text("""
            SELECT
                count(*) FILTER (WHERE revoked_at IS NULL AND expires_at <= statement_timestamp()),
                count(*) FILTER (WHERE revoked_at IS NOT NULL),
                count(*) FILTER (WHERE revoked_at IS NULL AND expires_at > statement_timestamp()),
                (SELECT count(*) FROM auth_rate_limits
                    WHERE window_start <= statement_timestamp() - interval '15 minutes'),
                (SELECT count(*) FROM auth_rate_limits
                    WHERE window_start > statement_timestamp() - interval '15 minutes')
            FROM auth_sessions
            """)
        )
        return tuple(result.one())


async def run_batch(engine: AsyncEngine) -> bool:
    async with engine.begin() as connection:
        return (
            await connection.scalar(text("SELECT public.cleanup_auth_batch()"))
        ) is True


async def test_cleanup_batches_skip_locked_rows_and_preserve_live_data(
    financial_engine: AsyncEngine,
) -> None:
    await seed_auth_state(financial_engine, 502)
    assert await auth_counts(financial_engine) == (502, 502, 1, 502, 1)
    assert await run_batch(financial_engine)
    assert await auth_counts(financial_engine) == (2, 2, 1, 2, 1)

    async with financial_engine.begin() as holder:
        await holder.execute(
            text("""
            SELECT id FROM auth_sessions WHERE expires_at <= statement_timestamp()
            ORDER BY id LIMIT 1 FOR UPDATE
            """)
        )
        async with asyncio.timeout(5):
            first, second = await asyncio.gather(
                run_batch(financial_engine), run_batch(financial_engine)
            )
        assert not first and not second
        assert await auth_counts(financial_engine) == (1, 0, 1, 0, 1)

    assert not await run_batch(financial_engine)
    assert not await run_batch(financial_engine)
    assert await auth_counts(financial_engine) == (0, 0, 1, 0, 1)


async def test_startup_cleanup_drains_multiple_batches(
    financial_engine: AsyncEngine,
    runtime_postgres: TestPostgres,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_auth_state(financial_engine, 501)
    monkeypatch.setenv(
        "FINANCEIQ_DATABASE_URL",
        runtime_postgres.url("financeiq_runtime", sqlalchemy=True),
    )
    monkeypatch.setenv("FINANCEIQ_APP_ENV", "test")
    app = create_app()
    async with app.router.lifespan_context(app), asyncio.timeout(5):
        while await auth_counts(financial_engine) != (0, 0, 1, 0, 1):
            await asyncio.sleep(0.02)
