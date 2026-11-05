"""Application factory without import-time configuration or server startup."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from financeiq.api.errors import database_error_handler
from financeiq.api.router import router
from financeiq.core.config import load_settings
from financeiq.db.errors import DATABASE_EXCEPTIONS, DatabaseError, finish_cleanup
from financeiq.db.session import create_engine, create_session_factory


def create_app(*, env_file: Path | None = None) -> FastAPI:
    """Create an independent application from explicitly loaded settings."""
    settings = load_settings(env_file=env_file)
    documentation_enabled = settings.app_env in ("development", "test")

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        application.state.engine = engine
        cancelled = False
        try:
            application.state.session_factory = create_session_factory(engine)
            yield
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
        docs_url="/api/v1/docs" if documentation_enabled else None,
        openapi_url="/api/v1/openapi.json" if documentation_enabled else None,
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
    )
    app.state.settings = settings
    app.add_exception_handler(DatabaseError, database_error_handler)
    app.include_router(router)
    return app
