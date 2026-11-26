"""Auth commands, client address policy and secure session cookies."""

import logging
from collections.abc import Callable, Coroutine
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from financeiq.api.dependencies import DatabaseSession
from financeiq.api.errors import ErrorResponse, ValidationErrorResponse, error_response
from financeiq.core.config import Settings
from financeiq.modules.auth.rate_limits import reserve_attempt
from financeiq.modules.auth.schemas import (
    LoginRequest,
    RegistrationRequest,
    UserProfile,
)
from financeiq.modules.auth.service import (
    SESSION_TTL,
    EmailAlreadyRegistered,
    InvalidCredentials,
    login_user,
    register_user,
)

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
_auth_errors: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Invalid JSON"},
    415: {"model": ErrorResponse, "description": "Unsupported media type"},
    422: {"model": ValidationErrorResponse, "description": "Invalid request fields"},
    429: {
        "model": ErrorResponse,
        "description": "Auth rate limit reached",
        "headers": {"Retry-After": {"schema": {"type": "integer", "minimum": 0}}},
    },
    500: {"model": ErrorResponse, "description": "Internal error"},
}


def _parse_ip(address: str) -> IPv4Address | IPv6Address:
    if "%" in address:
        raise ValueError("Invalid IP address.")
    return ip_address(address)


def _client_ip(request: Request, settings: Settings) -> bytes:
    if request.client is None:
        raise RuntimeError("Auth peer address is unavailable.")
    address = _parse_ip(request.client.host)
    if address in settings.auth_trusted_proxies:
        forwarded = request.headers.getlist("x-forwarded-for")
        if len(forwarded) == 1:
            try:
                address = _parse_ip(forwarded[0].strip())
            except ValueError:
                # Fall back to the connection's shared quota for a bad proxy header.
                pass
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.packed
    return address.packed


async def _admit(
    request: Request, session: AsyncSession, *, email: str | None = None
) -> None:
    settings: Settings = request.app.state.settings
    secret = settings.auth_rate_limit_secret
    if secret is None:
        raise RuntimeError("Auth rate limit secret is unavailable.")
    delay = await reserve_attempt(
        session,
        secret=bytes.fromhex(secret.get_secret_value()),
        ip=_client_ip(request, settings),
        email=email,
    )
    if delay is not None:
        raise HTTPException(status_code=429, headers={"Retry-After": str(delay)})


@router.post(
    "/register",
    status_code=201,
    responses=_auth_errors
    | {409: {"model": ErrorResponse, "description": "Email already registered"}},
)
async def register(
    payload: RegistrationRequest, session: DatabaseSession, request: Request
) -> UserProfile:
    await _admit(request, session)
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


@router.post(
    "/login",
    status_code=204,
    response_class=Response,
    responses=_auth_errors
    | {
        204: {
            "description": "Session created",
            "headers": {
                "Set-Cookie": {
                    "description": "Separate __Host-financeiq-session and __Host-financeiq-csrf cookies",
                    "schema": {"type": "string"},
                }
            },
        },
        401: {"model": ErrorResponse, "description": "Invalid credentials"},
    },
)
async def login(
    payload: LoginRequest, session: DatabaseSession, request: Request
) -> Response:
    await _admit(request, session, email=payload.email)
    try:
        issued = await login_user(
            session, email=payload.email, password=payload.password.get_secret_value()
        )
    except InvalidCredentials:
        raise HTTPException(status_code=401) from None

    response = Response(status_code=204)
    for name, value, httponly in (
        ("__Host-financeiq-session", issued.token, True),
        ("__Host-financeiq-csrf", issued.csrf_token, False),
    ):
        response.set_cookie(
            name,
            value,
            path="/",
            max_age=int(SESSION_TTL.total_seconds()),
            secure=True,
            httponly=httponly,
            samesite="strict",
        )
    return response
