"""Application factory without import-time configuration or server startup."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from financeiq.api.errors import (
    database_error_handler,
    http_error_handler,
    request_validation_error_handler,
)
from financeiq.api.router import API_PREFIX, create_router
from financeiq.core.config import load_settings
from financeiq.db.errors import DATABASE_EXCEPTIONS, DatabaseError, finish_cleanup
from financeiq.db.session import create_engine, create_session_factory
from financeiq.modules.auth.cleanup import run_auth_cleanup
from financeiq.modules.auth.router import AUTH_PREFIX, AuthResponseMiddleware


async def _stop_auth_cleanup(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def create_app(*, env_file: Path | None = None) -> FastAPI:
    """Create an independent application from explicitly loaded settings."""
    settings = load_settings(env_file=env_file)
    is_nonproduction = settings.app_env in ("development", "test")

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        application.state.engine = engine
        cancelled = False
        try:
            factory = create_session_factory(engine)
            application.state.session_factory = factory
            cleanup = asyncio.create_task(run_auth_cleanup(factory))
            try:
                yield
            finally:
                await finish_cleanup(_stop_auth_cleanup(cleanup))
        except asyncio.CancelledError:
            cancelled = True
            raise
        except DATABASE_EXCEPTIONS:
            raise DatabaseError() from None
        finally:
            try:
                await finish_cleanup(engine.dispose())
            except DatabaseError:
                if not cancelled:
                    raise

    app = FastAPI(
        title="FinanceIQ API",
        debug=False,
        lifespan=lifespan,
        docs_url="/api/v1/docs" if is_nonproduction else None,
        openapi_url="/api/v1/openapi.json" if is_nonproduction else None,
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
    )
    app.state.settings = settings
    app.add_exception_handler(DatabaseError, database_error_handler)
    app.add_exception_handler(HTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.add_middleware(AuthResponseMiddleware, prefix=API_PREFIX + AUTH_PREFIX)
    app.include_router(create_router(auth_enabled=is_nonproduction))
    return app
