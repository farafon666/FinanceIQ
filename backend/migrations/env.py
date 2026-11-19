"""Alembic boundary: independent credentials and sanitized database failures."""

import asyncio
from pathlib import Path

from alembic import context
from alembic.util import CommandError
from sqlalchemy.engine import Connection

from financeiq.core.config import load_migration_settings
from financeiq.db.errors import finish_cleanup
from financeiq.db.models import metadata
from financeiq.db.session import create_migration_engine


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    arguments = context.get_x_argument(as_dictionary=True)
    if set(arguments) - {"env_file"}:
        raise CommandError("Unknown migration configuration option.")
    env_file = Path(arguments["env_file"]) if "env_file" in arguments else None
    settings = load_migration_settings(env_file=env_file)
    engine = create_migration_engine(settings)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await finish_cleanup(engine.dispose())


try:
    if context.is_offline_mode():
        context.configure(
            dialect_name="postgresql",
            target_metadata=metadata,
            literal_binds=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    else:
        asyncio.run(run_online())
except Exception:
    # Include config, connection, DDL and cleanup errors, even with --raiseerr.
    # Deliberately do not include the input or driver exception in diagnostics.
    raise CommandError("Migration operation failed.") from None
