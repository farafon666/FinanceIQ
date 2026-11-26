"""Registration coordinates a single transaction across Auth and Categories."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from financeiq.db.session import transaction
from financeiq.modules.auth.passwords import hash_password
from financeiq.modules.auth.repository import insert_user
from financeiq.modules.categories.service import create_default_categories


class EmailAlreadyRegistered(Exception):
    """The canonical email is in use."""


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
