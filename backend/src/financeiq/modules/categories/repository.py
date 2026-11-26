"""Category persistence within the caller's transaction."""

from sqlalchemy.ext.asyncio import AsyncSession

from financeiq.modules.categories.models import Category


async def insert_categories(session: AsyncSession, categories: list[Category]) -> None:
    session.add_all(categories)
    await session.flush()
