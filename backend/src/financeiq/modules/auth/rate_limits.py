"""Reserve an Auth attempt before credential work in a short transaction."""

import hmac
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from financeiq.db.session import transaction
from financeiq.modules.auth.repository import (
    current_rate_limit_window,
    increment_rate_limit,
    retry_after_window,
)

_WINDOW_WIDTH = timedelta(minutes=15)


async def reserve_attempt(
    session: AsyncSession, *, secret: bytes, ip: bytes, email: str | None
) -> int | None:
    """Return the retry delay on rejection, after committing any IP charge."""
    scopes = (
        (("register_ip", ip, 5),)
        if email is None
        else (
            ("login_ip", ip, 30),
            ("login_email", email.encode("ascii"), 10),
        )
    )
    async with transaction(session):
        window_start = await current_rate_limit_window(session, _WINDOW_WIDTH)
        for scope, identifier, limit in scopes:
            key = hmac.digest(
                secret, scope.encode("ascii") + b"\0" + identifier, "sha256"
            )
            if not await increment_rate_limit(
                session, scope=scope, key=key, window_start=window_start, limit=limit
            ):
                return await retry_after_window(session, window_start + _WINDOW_WIDTH)
    return None
