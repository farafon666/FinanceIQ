"""Explicit Docker adapters; ordinary application configuration stays unchanged."""

import asyncio
import json
import os
import sys
from collections.abc import Sequence
from http.client import HTTPConnection
from pathlib import Path
from typing import Literal

from sqlalchemy import text
from sqlalchemy.engine import URL

from financeiq.core.config import Settings
from financeiq.db.errors import finish_cleanup
from financeiq.db.session import create_engine

SECRET_DIRECTORY = Path("/run/secrets")


def database_environment(role: Literal["runtime", "migrator"]) -> dict[str, str]:
    password = (SECRET_DIRECTORY / f"{role}_password").read_text(encoding="utf-8")
    password = password.rstrip("\r\n")
    if not password or any(character in password for character in "\r\n\0"):
        raise ValueError("Invalid password file")
    url = URL.create(
        "postgresql+asyncpg",
        username=f"financeiq_{role}",
        password=password,
        host="postgres",
        port=5432,
        database="financeiq",
    )
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in ("FINANCEIQ_DATABASE_URL", "FINANCEIQ_MIGRATION_DATABASE_URL")
    }
    key = (
        "FINANCEIQ_DATABASE_URL"
        if role == "runtime"
        else "FINANCEIQ_MIGRATION_DATABASE_URL"
    )
    environment[key] = url.render_as_string(hide_password=False)
    return environment


def check_http() -> None:
    connection = HTTPConnection("127.0.0.1", 8000, timeout=2)
    try:
        connection.request("GET", "/api/v1/health")
        response = connection.getresponse()
        if response.status != 200 or json.loads(response.read(128)) != {"status": "ok"}:
            raise ValueError("HTTP check failed")
    finally:
        connection.close()


async def check_database(settings: Settings) -> None:
    async with asyncio.timeout(5):
        engine = create_engine(settings)
        try:
            async with engine.connect() as connection:
                if await connection.scalar(text("SELECT 1")) != 1:
                    raise ValueError("Database check failed")
        finally:
            await finish_cleanup(engine.dispose())


def main(arguments: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    try:
        if arguments == ["ready"]:
            environment = database_environment("runtime")
            settings = Settings(database_url=environment["FINANCEIQ_DATABASE_URL"])
            check_http()
            asyncio.run(check_database(settings))
        elif arguments == ["serve"]:
            environment = database_environment("runtime")
            os.execve(
                sys.executable,
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "financeiq.main:create_app",
                    "--factory",
                    "--no-access-log",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8000",
                ],
                environment,
            )
        elif arguments and arguments[0] == "migrate":
            environment = database_environment("migrator")
            os.execve(
                sys.executable,
                [
                    sys.executable,
                    "-m",
                    "alembic",
                    *(arguments[1:] or ["upgrade", "head"]),
                ],
                environment,
            )
        else:
            raise ValueError("Unknown container command")
    except Exception:
        # Never print file contents, SQL, connection URLs or exception chains.
        print("Container command failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
