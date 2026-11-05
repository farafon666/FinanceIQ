"""Safe database errors and cancellation-safe resource cleanup."""

import asyncio
from collections.abc import Coroutine
from typing import Any

from asyncpg import InterfaceError, PostgresError
from sqlalchemy.exc import SQLAlchemyError


class DatabaseError(RuntimeError):
    """Database failure without driver messages, SQL, parameters or credentials."""

    def __init__(self) -> None:
        super().__init__("Database operation failed.")


DATABASE_EXCEPTIONS = (
    DatabaseError,
    SQLAlchemyError,
    PostgresError,
    InterfaceError,
    OSError,
)


async def finish_cleanup(operation: Coroutine[Any, Any, None]) -> None:
    """Drain cleanup even if its caller is cancelled; never orphan a failing task."""
    task = asyncio.create_task(operation)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = error
        except Exception:
            # Retrieve the result below, after giving cancellation precedence.
            break
    try:
        task.result()
    except Exception:
        if cancellation is None:
            raise DatabaseError() from None
    if cancellation is not None:
        raise cancellation
