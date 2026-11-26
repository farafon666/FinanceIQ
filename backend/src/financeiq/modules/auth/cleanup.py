"""Periodic cleanup of disposable Auth state in short transactions."""

import logging
from asyncio import get_running_loop, sleep

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financeiq.db.session import session_scope, transaction
from financeiq.modules.auth.repository import cleanup_auth_batch

_logger = logging.getLogger(__name__)
_CLEANUP_INTERVAL_SECONDS = 3600


async def run_auth_cleanup(factory: async_sessionmaker[AsyncSession]) -> None:
    clock = get_running_loop().time
    while True:
        next_run = clock() + _CLEANUP_INTERVAL_SECONDS
        try:
            more = True
            while more:
                async with session_scope(factory) as session, transaction(session):
                    more = await cleanup_auth_batch(session)
        except Exception:
            _logger.error("auth_cleanup_error")
        await sleep(max(0, next_run - clock()))
