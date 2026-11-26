"""Exercise Auth maintenance scheduling and shutdown without a database."""

import asyncio
import logging
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from financeiq.main import create_app
from financeiq.modules.auth import cleanup


@pytest.mark.asyncio
async def test_cleanup_failure_retries_without_blocking_http_and_stops_before_dispose(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(
        "FINANCEIQ_DATABASE_URL",
        "postgresql+asyncpg://test:unused@127.0.0.1:5432/financeiq_test",
    )
    monkeypatch.setenv("FINANCEIQ_APP_ENV", "development")
    monkeypatch.setattr("financeiq.main.run_auth_cleanup", cleanup.run_auth_cleanup)

    waiting = asyncio.Event()
    hourly_tick = asyncio.Event()
    running_batch = asyncio.Event()
    dispose = AsyncMock()
    sessions: list[AsyncSession] = []
    completed: list[str] = []

    async def batch(session: AsyncSession) -> bool:
        sessions.append(session)
        assert session.in_transaction()
        if len(sessions) == 1:
            raise RuntimeError("private-cleanup-failure")
        running_batch.set()
        try:
            await asyncio.Future[None]()
        finally:
            dispose.assert_not_awaited()
            completed.append("batch cancelled")
        return False

    async def sleep(delay: float) -> None:
        assert 3599 <= delay <= 3600
        waiting.set()
        await hourly_tick.wait()

    monkeypatch.setattr(cleanup, "cleanup_auth_batch", batch)
    monkeypatch.setattr(cleanup, "sleep", sleep)
    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    app = create_app()
    async with (
        asyncio.timeout(5),
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client,
    ):
        await waiting.wait()
        response = await client.get("http://test/api/v1/health")
        assert response.json() == {"status": "ok"}
        assert not sessions[0].in_transaction()
        hourly_tick.set()
        await running_batch.wait()

    assert len(sessions) == 2 and sessions[0] is not sessions[1]
    assert not sessions[1].in_transaction()
    assert completed == ["batch cancelled"]
    dispose.assert_awaited_once()
    assert [r.message for r in caplog.records if r.levelno >= logging.ERROR] == [
        "auth_cleanup_error"
    ]
    assert "private-cleanup-failure" not in caplog.text
