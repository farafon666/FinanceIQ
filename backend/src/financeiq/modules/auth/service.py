"""Registration and session issuance own the Auth command transactions."""

from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from datetime import timedelta
from hashlib import sha256
from secrets import token_bytes
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from financeiq.db.session import transaction
from financeiq.modules.auth.passwords import hash_password, verify_password
from financeiq.modules.auth.repository import (
    find_active_credentials,
    insert_auth_session,
    insert_user,
    lock_active_credentials,
    update_password_hash,
)
from financeiq.modules.categories.service import create_default_categories


class EmailAlreadyRegistered(Exception):
    """The canonical email is in use."""


class InvalidCredentials(Exception):
    """No active user with the verified credentials."""


SESSION_TTL = timedelta(hours=12)


@dataclass(frozen=True)
class IssuedSession:
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)


async def register_user(
    session: AsyncSession, *, email: str, password: str, timezone: str
) -> UUID:
    password_hash = await hash_password(password)
    async with transaction(session):
        user_id = await insert_user(
            session, email=email, password_hash=password_hash, timezone=timezone
        )
        if user_id is None:
            raise EmailAlreadyRegistered()
        await create_default_categories(session, user_id)
    return user_id


async def login_user(
    session: AsyncSession, *, email: str, password: str
) -> IssuedSession:
    async with transaction(session):
        credentials = await find_active_credentials(session, email)
    if credentials is None:
        await verify_password(password, None)
        raise InvalidCredentials()

    user_id, password_hash = credentials
    for _ in range(2):
        verified, replacement = await verify_password(password, password_hash)
        if not verified:
            raise InvalidCredentials()

        async with transaction(session):
            current_hash = await lock_active_credentials(session, user_id)
            if current_hash is None:
                raise InvalidCredentials()
            if current_hash != password_hash:
                password_hash = current_hash
                continue
            if replacement is not None:
                await update_password_hash(session, user_id, replacement)
            token, csrf_token = token_bytes(32), token_bytes(32)
            await insert_auth_session(
                session,
                user_id,
                sha256(token).digest(),
                sha256(csrf_token).digest(),
                SESSION_TTL,
            )
        return IssuedSession(_encode_secret(token), _encode_secret(csrf_token))
    raise InvalidCredentials()


def _encode_secret(secret: bytes) -> str:
    return urlsafe_b64encode(secret).rstrip(b"=").decode("ascii")
