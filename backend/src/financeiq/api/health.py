"""Public liveness check without financial data or infrastructure probes."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class HealthResponse(BaseModel):
    """The complete public health response."""

    status: Literal["ok"]


@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health_check() -> HealthResponse:
    """Confirm that the HTTP application can respond to a request."""
    return HealthResponse(status="ok")
