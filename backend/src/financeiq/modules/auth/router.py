"""Auth commands, client address policy and secure session cookies."""

import logging
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from financeiq.api.dependencies import DatabaseSession
from financeiq.api.errors import (
    AuthenticationRequired,
    ErrorResponse,
    ValidationErrorResponse,
    error_response,
)
from financeiq.api.origin import SameOriginRoute
from financeiq.core.config import Settings
from financeiq.modules.auth.dependencies import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    CurrentSession,
    CurrentWriteSession,
)
from financeiq.modules.auth.rate_limits import reserve_attempt
from financeiq.modules.auth.schemas import (
    LoginRequest,
    RegistrationRequest,
    TimezoneUpdateRequest,
    UserProfile,
)
from financeiq.modules.auth.service import (
    SESSION_TTL,
    EmailAlreadyRegistered,
    InvalidCredentials,
    change_timezone,
    login_user,
    logout_user,
    register_user,
)

_logger = logging.getLogger(__name__)
AUTH_PREFIX = "/auth"


class JsonRoute(SameOriginRoute):
    async def validate_request(self, request: Request) -> None:
        await super().validate_request(request)
        content_type = request.headers.get("content-type")
        if content_type is None:
            if await request.body():
                raise HTTPException(status_code=415)
        elif content_type.partition(";")[0].strip().lower() != "application/json":
            raise HTTPException(status_code=415)


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


router = APIRouter(prefix=AUTH_PREFIX, tags=["auth"], route_class=SameOriginRoute)
_json_router = APIRouter(route_class=JsonRoute)
_internal_error = {"model": ErrorResponse, "description": "Internal error"}
_forbidden_origin = {"model": ErrorResponse, "description": "Origin rejected"}
_forbidden_write = {
    "model": ErrorResponse,
    "description": "Origin or CSRF token rejected",
}
_json_errors: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Invalid JSON"},
    415: {"model": ErrorResponse, "description": "Unsupported media type"},
    422: {"model": ValidationErrorResponse, "description": "Invalid request fields"},
}
_auth_errors = _json_errors | {
    403: _forbidden_origin,
    429: {
        "model": ErrorResponse,
        "description": "Auth rate limit reached",
        "headers": {"Retry-After": {"schema": {"type": "integer", "minimum": 0}}},
    },
    500: _internal_error,
}
_session_errors: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Authentication required"},
    500: _internal_error,
}
_csrf_openapi = {
    "parameters": [
        {
            "name": CSRF_HEADER,
            "in": "header",
            "required": True,
            "schema": {"type": "string"},
        }
    ]
}
_cookie_headers = {
    "Set-Cookie": {
        "description": "Separate __Host-financeiq-session and __Host-financeiq-csrf cookies",
        "schema": {"type": "string"},
    }
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


@_json_router.post(
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


@_json_router.post(
    "/login",
    status_code=204,
    response_class=Response,
    responses=_auth_errors
    | {
        204: {"description": "Session created", "headers": _cookie_headers},
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

    return _cookie_response(
        issued.token, issued.csrf_token, max_age=int(SESSION_TTL.total_seconds())
    )


def _cookie_response(token: str, csrf_token: str, *, max_age: int) -> Response:
    response = Response(status_code=204)
    for name, value, httponly in (
        (SESSION_COOKIE, token, True),
        (CSRF_COOKIE, csrf_token, False),
    ):
        response.set_cookie(
            name,
            value,
            path="/",
            max_age=max_age,
            secure=True,
            httponly=httponly,
            samesite="strict",
        )
    return response


@router.get("/me", responses=_session_errors)
async def me(current: CurrentSession) -> UserProfile:
    return UserProfile(
        id=current.user_id, email=current.email, timezone=current.timezone
    )


@_json_router.patch(
    "/me",
    responses=_json_errors | _session_errors | {403: _forbidden_write},
    openapi_extra=_csrf_openapi,
)
async def patch_timezone(
    payload: TimezoneUpdateRequest,
    current: CurrentWriteSession,
    session: DatabaseSession,
) -> UserProfile:
    profile = await change_timezone(
        session, user_id=current.user_id, timezone=payload.timezone
    )
    if profile is None:
        raise AuthenticationRequired()
    user_id, email, timezone = profile
    return UserProfile(id=user_id, email=email, timezone=timezone)


@router.post(
    "/logout",
    status_code=204,
    response_class=Response,
    responses=_session_errors
    | {
        204: {"description": "Session revoked", "headers": _cookie_headers},
        403: _forbidden_write,
    },
    openapi_extra=_csrf_openapi,
)
async def logout(current: CurrentWriteSession, session: DatabaseSession) -> Response:
    if not await logout_user(session, current.id):
        raise AuthenticationRequired()
    return _cookie_response("", "", max_age=0)


router.include_router(_json_router)
