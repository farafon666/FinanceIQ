"""Persistence for Auth commands using the caller's transaction."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import DateTime, extract, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from financeiq.modules.auth.models import AuthRateLimit, AuthSession, User


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


async def find_active_credentials(
    session: AsyncSession, email: str
) -> tuple[UUID, str] | None:
    result = await session.execute(
        select(User.id, User.password_hash).where(User.email == email, User.is_active)
    )
    return result.tuples().one_or_none()


async def lock_active_credentials(session: AsyncSession, user_id: UUID) -> str | None:
    password_hash: str | None = await session.scalar(
        select(User.password_hash)
        .where(User.id == user_id, User.is_active)
        .with_for_update(key_share=True)
    )
    return password_hash


async def update_password_hash(
    session: AsyncSession, user_id: UUID, password_hash: str
) -> None:
    await session.execute(
        update(User).where(User.id == user_id).values(password_hash=password_hash)
    )


async def insert_auth_session(
    session: AsyncSession,
    user_id: UUID,
    token_hash: bytes,
    csrf_token_hash: bytes,
    ttl: timedelta,
) -> None:
    clock = select(func.clock_timestamp(type_=DateTime(timezone=True)))
    created_at = (await session.execute(clock)).scalar_one()
    await session.execute(
        insert(AuthSession).values(
            user_id=user_id,
            created_at=created_at,
            expires_at=created_at + ttl,
            token_hash=token_hash,
            csrf_token_hash=csrf_token_hash,
        )
    )


async def current_rate_limit_window(
    session: AsyncSession, width: timedelta
) -> datetime:
    start = select(
        func.date_bin(
            width,
            func.clock_timestamp(),
            datetime(1970, 1, 1, tzinfo=UTC),
            type_=DateTime(timezone=True),
        )
    )
    return (await session.execute(start)).scalar_one()


async def increment_rate_limit(
    session: AsyncSession, *, scope: str, key: bytes, window_start: datetime, limit: int
) -> bool:
    statement = (
        insert(AuthRateLimit)
        .values(scope=scope, key_digest=key, window_start=window_start, attempt_count=1)
        .on_conflict_do_update(
            index_elements=[
                AuthRateLimit.scope,
                AuthRateLimit.key_digest,
                AuthRateLimit.window_start,
            ],
            set_={"attempt_count": AuthRateLimit.attempt_count + 1},
            where=AuthRateLimit.attempt_count < limit,
        )
        .returning(AuthRateLimit.attempt_count)
    )
    return await session.scalar(statement) is not None


async def retry_after_window(session: AsyncSession, window_end: datetime) -> int:
    remaining = extract(
        "epoch",
        literal(window_end, type_=DateTime(timezone=True)) - func.clock_timestamp(),
    )
    result = await session.execute(select(func.greatest(0, func.ceil(remaining))))
    return int(result.scalar_one())
