"""Registration through FastAPI and the migrated PostgreSQL runtime role."""

import asyncio
import logging
from collections.abc import AsyncIterator
from unicodedata import normalize
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from argon2 import PasswordHasher, Type, extract_parameters
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from financeiq.main import create_app
from financeiq.modules.auth.models import AuthSession, User
from financeiq.modules.categories import service as categories_service
from financeiq.modules.categories.models import CategorizationRule, Category
from financeiq.modules.categories.repository import insert_categories
from tests.support.postgres import TestPostgres

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

REGISTER = "/api/v1/auth/register"
PASSWORD = " e\u0301" + "x" * 12 + "Z"
REQUEST = {
    "email": "  Test.Name+Work@EXAMPLE.ORG  ",
    "password": PASSWORD,
    "timezone": "Europe/Saratov",
}
EXPECTED = {
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
}


@pytest_asyncio.fixture
async def client(
    runtime_postgres: TestPostgres, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("FINANCEIQ_APP_ENV", "test")
    monkeypatch.setenv("FINANCEIQ_AUTH_RATE_LIMIT_SECRET", "ab" * 32)
    monkeypatch.setenv(
        "FINANCEIQ_DATABASE_URL",
        runtime_postgres.url("financeiq_runtime", sqlalchemy=True),
    )
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as test_client,
    ):
        yield test_client


async def test_registration_saves_personal_categories_and_argon2id(
    client: httpx.AsyncClient, financial_engine: AsyncEngine
) -> None:
    first = await client.post(
        REGISTER,
        json=REQUEST,
        headers={"Content-Type": "Application/JSON; charset=utf-8"},
    )
    second = await client.post(
        REGISTER,
        json=REQUEST | {"email": "other@example.org", "password": "e\u0301" * 128},
    )
    user_ids = []
    for response, email in (
        (first, "test.name+work@example.org"),
        (second, "other@example.org"),
    ):
        assert response.status_code == 201
        assert response.headers["cache-control"] == "no-store"
        assert "set-cookie" not in response.headers
        body = response.json()
        assert body == {"id": body["id"], "email": email, "timezone": "Europe/Saratov"}
        user_ids.append(UUID(body["id"]))

    async with AsyncSession(financial_engine) as observer:
        first_user = await observer.get(User, user_ids[0])
        second_user = await observer.get(User, user_ids[1])
        assert first_user is not None and second_user is not None
        assert first_user.is_active and second_user.is_active
        assert first_user.email == first.json()["email"]
        assert first_user.timezone == REQUEST["timezone"]

        password_hasher = PasswordHasher()
        parameters = extract_parameters(first_user.password_hash)
        assert parameters.type is Type.ID
        assert (
            parameters.memory_cost,
            parameters.time_cost,
            parameters.parallelism,
        ) == (19456, 2, 1)
        assert password_hasher.verify(
            first_user.password_hash, normalize("NFC", PASSWORD)
        )
        assert password_hasher.verify(second_user.password_hash, "é" * 128)

        category_ids: set[UUID] = set()
        for owner in user_ids:
            categories = list(
                await observer.scalars(
                    select(Category).where(Category.user_id == owner)
                )
            )
            assert len(categories) == 10
            assert {
                (category.kind, category.name) for category in categories
            } == EXPECTED
            assert all(
                category.archived_at is None
                and category.name_key == category.name.casefold()
                for category in categories
            )
            category_ids.update(category.id for category in categories)
        assert len(category_ids) == 20
        assert await observer.scalar(select(AuthSession.id)) is None
        assert await observer.scalar(select(CategorizationRule.id)) is None


async def test_concurrent_and_repeated_email_conflicts_leave_one_category_set(
    client: httpx.AsyncClient, financial_engine: AsyncEngine
) -> None:
    same_email = REQUEST | {"email": "test.name+work@example.org"}
    first, second = await asyncio.gather(
        client.post(REGISTER, json=REQUEST), client.post(REGISTER, json=same_email)
    )
    repeat = await client.post(REGISTER, json=same_email)
    assert sorted([first.status_code, second.status_code]) == [201, 409]
    success, conflict = (first, second) if first.status_code == 201 else (second, first)
    assert repeat.status_code == 409
    assert conflict.json() == repeat.json()
    assert conflict.json()["error"]["code"] == "conflict"
    assert (
        conflict.headers["cache-control"]
        == repeat.headers["cache-control"]
        == "no-store"
    )
    async with AsyncSession(financial_engine) as observer:
        owner = UUID(success.json()["id"])
        assert list(await observer.scalars(select(User.id))) == [owner]
        assert list(await observer.scalars(select(Category.user_id))) == [owner] * 10


async def test_category_failure_rolls_back_registration(
    client: httpx.AsyncClient,
    financial_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail_after_first_insert(
        session: AsyncSession, categories: list[Category]
    ) -> None:
        await insert_categories(session, categories[:1])
        raise RuntimeError("private-category-failure-marker")

    with monkeypatch.context() as patch, caplog.at_level(logging.DEBUG):
        patch.setattr(categories_service, "insert_categories", fail_after_first_insert)
        response = await client.post(REGISTER, json=REQUEST)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert response.headers["cache-control"] == "no-store"
    for private in (*REQUEST.values(), "private-category-failure-marker", "Traceback"):
        assert private not in response.text + caplog.text

    async with AsyncSession(financial_engine) as observer:
        assert await observer.scalar(select(User.id)) is None
        assert await observer.scalar(select(Category.id)) is None
    assert (await client.post(REGISTER, json=REQUEST)).status_code == 201
