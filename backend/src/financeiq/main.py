"""Application factory without import-time configuration or server startup."""

from pathlib import Path

from fastapi import FastAPI

from financeiq.api.router import router
from financeiq.core.config import load_settings


def create_app(*, env_file: Path | None = None) -> FastAPI:
    """Create an independent application from explicitly loaded settings."""
    settings = load_settings(env_file=env_file)
    documentation_enabled = settings.app_env in ("development", "test")
    app = FastAPI(
        title="FinanceIQ API",
        debug=False,
        docs_url="/api/v1/docs" if documentation_enabled else None,
        openapi_url="/api/v1/openapi.json" if documentation_enabled else None,
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
    )
    app.state.settings = settings
    app.include_router(router)
    return app
