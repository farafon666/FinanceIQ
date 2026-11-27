"""Shared entry point for versioned HTTP routes."""

from fastapi import APIRouter

from financeiq.api.health import router as health_router
from financeiq.modules.auth.router import router as auth_router

API_PREFIX = "/api/v1"


def create_router() -> APIRouter:
    router = APIRouter(prefix=API_PREFIX)
    router.include_router(health_router)
    router.include_router(auth_router)
    return router
