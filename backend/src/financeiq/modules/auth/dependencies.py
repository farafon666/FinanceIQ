"""Resolve the authenticated session for protected HTTP routes."""

from typing import Annotated

from fastapi import Depends, Security
from fastapi.security import APIKeyCookie

from financeiq.api.dependencies import DatabaseSession
from financeiq.api.errors import AuthenticationRequired
from financeiq.modules.auth.service import AuthenticatedSession, authenticate_session

SESSION_COOKIE = "__Host-financeiq-session"
_session_cookie = APIKeyCookie(name=SESSION_COOKIE, auto_error=False)


async def get_current_session(
    session: DatabaseSession, token: Annotated[str | None, Security(_session_cookie)]
) -> AuthenticatedSession:
    current = await authenticate_session(session, token)
    if current is None:
        raise AuthenticationRequired()
    return current


CurrentSession = Annotated[AuthenticatedSession, Depends(get_current_session)]
