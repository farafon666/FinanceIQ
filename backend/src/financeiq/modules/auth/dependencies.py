"""Resolve the authenticated session and verify CSRF on protected writes."""

from hmac import compare_digest
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyCookie

from financeiq.api.dependencies import DatabaseSession
from financeiq.api.errors import AuthenticationRequired
from financeiq.modules.auth.domain import token_digest
from financeiq.modules.auth.service import AuthenticatedSession, authenticate_session

SESSION_COOKIE = "__Host-financeiq-session"
CSRF_COOKIE = "__Host-financeiq-csrf"
CSRF_HEADER = "X-CSRF-Token"
_session_cookie = APIKeyCookie(name=SESSION_COOKIE, auto_error=False)


async def get_current_session(
    session: DatabaseSession, token: Annotated[str | None, Security(_session_cookie)]
) -> AuthenticatedSession:
    current = await authenticate_session(session, token)
    if current is None:
        raise AuthenticationRequired()
    return current


CurrentSession = Annotated[AuthenticatedSession, Depends(get_current_session)]


async def get_write_session(
    request: Request, current: CurrentSession
) -> AuthenticatedSession:
    values = request.headers.getlist(CSRF_HEADER)
    digest = token_digest(values[0]) if len(values) == 1 else None
    if digest is None or not compare_digest(digest, current.csrf_token_hash):
        raise HTTPException(status_code=403)
    return current


CurrentWriteSession = Annotated[AuthenticatedSession, Depends(get_write_session)]
