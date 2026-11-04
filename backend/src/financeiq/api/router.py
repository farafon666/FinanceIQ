"""Shared entry point for versioned HTTP routes."""

from fastapi import APIRouter

from financeiq.api.health import router as health_router

router = APIRouter(prefix="/api/v1")
router.include_router(health_router)
