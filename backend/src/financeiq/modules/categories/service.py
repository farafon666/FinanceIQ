"""Category initialization inside the registration transaction."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from financeiq.modules.categories.domain import normalize_category_name
from financeiq.modules.categories.models import Category
from financeiq.modules.categories.repository import insert_categories

_DEFAULT_CATEGORIES_V1 = (
    ("income", "Зарплата"),
    ("income", "Подработка"),
    ("income", "Проценты"),
    ("expense", "Продукты"),
    ("expense", "Кафе и рестораны"),
    ("expense", "Транспорт"),
    ("expense", "Жилье и коммунальные услуги"),
    ("expense", "Здоровье"),
    ("expense", "Покупки"),
    ("expense", "Досуг"),
)


async def create_default_categories(session: AsyncSession, user_id: UUID) -> None:
    categories: list[Category] = []
    for kind, default_name in _DEFAULT_CATEGORIES_V1:
        name, name_key = normalize_category_name(default_name)
        categories.append(
            Category(user_id=user_id, kind=kind, name=name, name_key=name_key)
        )
    await insert_categories(session, categories)
