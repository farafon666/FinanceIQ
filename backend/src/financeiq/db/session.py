"""Application-owned async engine, request sessions and service transactions."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.engine import Dialect, ExceptionContext
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry

from financeiq.core.config import Settings
from financeiq.db.errors import DATABASE_EXCEPTIONS, DatabaseError, finish_cleanup


def create_engine(settings: Settings) -> AsyncEngine:
    """Create a lazy engine with isolated, silenced raw engine/pool loggers."""
    name = f"financeiq_{uuid4().hex}"
    try:
        engine = create_async_engine(
            settings.database_url.get_secret_value(),
            isolation_level="READ COMMITTED",
            pool_pre_ping=True,
            connect_args={"timeout": 5},
            echo=False,
            echo_pool=False,
            hide_parameters=True,
            logging_name=name,
            pool_logging_name=name,
        )
    except Exception:
        raise DatabaseError() from None
    # echo=False alone still permits raw SQL when an ancestor logger is DEBUG.
    # Configure only these instance loggers; leave root and other engines alone.
    for prefix in (
        "sqlalchemy.engine.Engine",
        "sqlalchemy.pool.impl.AsyncAdaptedQueuePool",
    ):
        logging.getLogger(f"{prefix}.{name}").disabled = True
    event.listen(
        engine.sync_engine, "handle_error", _unexpected_driver_error, retval=True
    )
    event.listen(engine.sync_engine, "do_connect", _connect)
    return engine


def _connect(
    dialect: Dialect,
    record: ConnectionPoolEntry,
    args: list[Any],
    parameters: dict[str, Any],
) -> DBAPIConnection:
    # SQLAlchemy's handle_error hook does not see non-DBAPI connection errors.
    # This boundary contains only driver work, never service/business failures.
    try:
        return dialect.connect(*args, **parameters)
    except Exception:
        raise DatabaseError() from None


def _unexpected_driver_error(context: ExceptionContext) -> Exception | None:
    # Cancellation and other control-flow BaseExceptions are not DB failures.
    if not isinstance(context.original_exception, Exception):
        return None
    # Drivers can raise ValueError/TypeError for connection options. Sanitize
    # these at the SQL execution boundary without hiding service ValueErrors.
    # Known DB exceptions remain available for domain handling inside services.
    if isinstance(context.original_exception, DATABASE_EXCEPTIONS):
        return None
    return DatabaseError()


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine, autobegin=False, autoflush=False, expire_on_commit=False
    )


@asynccontextmanager
async def session_scope(
    factory: Callable[[], AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Supply a session without committing; close before leaving the scope."""
    try:
        session = factory()
    except DATABASE_EXCEPTIONS:
        raise DatabaseError() from None
    failure: BaseException | None = None
    try:
        yield session
    except BaseException as error:
        failure = error
        if isinstance(error, DATABASE_EXCEPTIONS):
            raise DatabaseError() from None
        raise
    finally:
        try:
            await finish_cleanup(session.close())
        except DatabaseError:
            if not isinstance(failure, asyncio.CancelledError):
                raise


@asynccontextmanager
async def transaction(session: AsyncSession) -> AsyncIterator[None]:
    """The outer service owns one commit; any failure rolls back without retry."""
    try:
        unit = await session.begin()
        yield
        await unit.commit()
    except BaseException as error:
        try:
            await finish_cleanup(session.rollback())
        except DatabaseError:
            if not isinstance(error, asyncio.CancelledError):
                raise
        if isinstance(error, DATABASE_EXCEPTIONS):
            raise DatabaseError() from None
        raise
