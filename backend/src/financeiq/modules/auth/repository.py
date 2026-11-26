"""Persistence for Auth commands using the caller's transaction."""

from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from financeiq.modules.auth.models import User


async def insert_user(
    session: AsyncSession, *, email: str, password_hash: str, timezone: str
) -> UUID | None:
    statement = (
        insert(User)
        .values(email=email, password_hash=password_hash, timezone=timezone)
        .on_conflict_do_nothing(index_elements=[User.email])
        .returning(User.id)
    )
    user_id: UUID | None = await session.scalar(statement)
    return user_id
