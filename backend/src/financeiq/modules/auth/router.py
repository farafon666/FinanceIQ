"""Registration HTTP endpoint and Auth response policy."""

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.routing import APIRoute
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from financeiq.api.dependencies import DatabaseSession
from financeiq.api.errors import ErrorResponse, ValidationErrorResponse, error_response
from financeiq.modules.auth.schemas import RegistrationRequest, UserProfile
from financeiq.modules.auth.service import EmailAlreadyRegistered, register_user

_logger = logging.getLogger(__name__)
AUTH_PREFIX = "/auth"


class JsonRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handle = super().get_route_handler()

        async def require_json(request: Request) -> Response:
            content_type = request.headers.get("content-type")
            if content_type is None:
                if await request.body():
                    return error_response(415)
            elif content_type.partition(";")[0].strip().lower() != "application/json":
                return error_response(415)
            return await handle(request)

        return require_json


class AuthResponseMiddleware:
    def __init__(self, app: ASGIApp, *, prefix: str) -> None:
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or (
            scope["path"] != self.prefix
            and not scope["path"].startswith(self.prefix + "/")
        ):
            await self.app(scope, receive, send)
            return

        started = False

        async def send_auth(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        try:
            await self.app(scope, receive, send_auth)
        except Exception:
            _logger.error("internal_error")
            if started:
                raise RuntimeError("Auth response failed.") from None
            await error_response(500)(scope, receive, send_auth)


router = APIRouter(prefix=AUTH_PREFIX, tags=["auth"], route_class=JsonRoute)
_registration_errors: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Invalid JSON"},
    409: {"model": ErrorResponse, "description": "Email already registered"},
    415: {"model": ErrorResponse, "description": "Unsupported media type"},
    422: {"model": ValidationErrorResponse, "description": "Invalid request fields"},
    500: {"model": ErrorResponse, "description": "Internal error"},
}


@router.post("/register", status_code=201, responses=_registration_errors)
async def register(
    payload: RegistrationRequest, session: DatabaseSession
) -> UserProfile:
    try:
        user_id = await register_user(
            session,
            email=payload.email,
            password=payload.password.get_secret_value(),
            timezone=payload.timezone,
        )
    except EmailAlreadyRegistered:
        raise HTTPException(status_code=409) from None
    return UserProfile(id=user_id, email=payload.email, timezone=payload.timezone)
