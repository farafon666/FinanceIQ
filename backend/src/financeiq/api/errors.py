"""Minimal technical error boundary; domain HTTP contracts arrive in stage 3."""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

_logger = logging.getLogger(__name__)


async def database_error_handler(request: Request, error: Exception) -> JSONResponse:
    _logger.error("database_error")
    return JSONResponse(
        status_code=500, content={"detail": "Database operation failed."}
    )
