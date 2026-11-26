"""Exercise Auth commands against PostgreSQL as the runtime role."""

import asyncio
import hmac
from base64 import urlsafe_b64decode
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from hashlib import sha256
from http.cookies import SimpleCookie
from ipaddress import ip_address
from unittest.mock import AsyncMock

import httpx
import pytest
from argon2 import PasswordHasher, extract_parameters
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from financeiq.main import create_app
from financeiq.modules.auth import service
from financeiq.modules.auth.models import AuthRateLimit, AuthSession, User
from tests.support.postgres import TestPostgres

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.asyncio,
    pytest.mark.usefixtures("auth_environment"),
]

SECRET = bytes.fromhex("a3" * 32)
EMAIL = "test.name+work@example.org"
LOGIN = "/api/v1/auth/login"
REGISTER = "/api/v1/auth/register"
SESSION_COOKIE = "__Host-financeiq-session"
CSRF_COOKIE = "__Host-financeiq-csrf"


@pytest.fixture
def auth_environment(
    runtime_postgres: TestPostgres, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FINANCEIQ_APP_ENV", "test")
    monkeypatch.setenv(
        "FINANCEIQ_DATABASE_URL",
        runtime_postgres.url("financeiq_runtime", sqlalchemy=True),
    )
    monkeypatch.setenv("FINANCEIQ_AUTH_RATE_LIMIT_SECRET", SECRET.hex())
    monkeypatch.delenv("FINANCEIQ_AUTH_TRUSTED_PROXIES", raising=False)


@asynccontextmanager
async def auth_client(ip: str = "192.0.2.1") -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(ip, 12345)),
            base_url="https://test",
        ) as client,
    ):
        yield client


def session_secrets(response: httpx.Response) -> tuple[bytes, bytes]:
    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    cookies = SimpleCookie()
    headers = response.headers.get_list("set-cookie")
    assert len(headers) == 2
    for header in headers:
        cookies.load(header)
    assert set(cookies) == {SESSION_COOKIE, CSRF_COOKIE}
    for name, cookie in cookies.items():
        assert cookie["secure"]
        assert bool(cookie["httponly"]) == (name == SESSION_COOKIE)
        assert cookie["samesite"].lower() == "strict"
        assert cookie["path"] == "/"
        assert cookie["max-age"] == "43200"
        assert "domain=" not in cookie.OutputString().lower()
        assert len(cookie.value) == 43 and "=" not in cookie.value
    token = urlsafe_b64decode(cookies[SESSION_COOKIE].value + "=")
    csrf_token = urlsafe_b64decode(cookies[CSRF_COOKIE].value + "=")
    assert token != csrf_token and len(token) == len(csrf_token) == 32
    return token, csrf_token


async def test_login_rehashes_and_persists_two_independent_sessions(
    financial_engine: AsyncEngine,
) -> None:
    password = " Pre\u0301serve This Password "
    normalized = " Préserve This Password "
    old_hash = PasswordHasher(memory_cost=8192, time_cost=2, parallelism=1).hash(
        normalized
    )
    async with financial_engine.begin() as connection:
        user_id = await connection.scalar(
            insert(User)
            .values(email=EMAIL, password_hash=old_hash, timezone="UTC")
            .returning(User.id)
        )

    async with auth_client() as first, auth_client() as second:
        payload = {"email": "  Test.Name+Work@EXAMPLE.ORG  ", "password": password}
        responses = await asyncio.gather(
            first.post(LOGIN, json=payload), second.post(LOGIN, json=payload)
        )
        pairs = [session_secrets(response) for response in responses]
        for wrong_password in (normalized.strip(), normalized.lower()):
            response = await first.post(
                LOGIN, json=payload | {"password": wrong_password}
            )
            assert response.status_code == 401
    assert len({secret for pair in pairs for secret in pair}) == 4

    async with AsyncSession(financial_engine) as observer:
        sessions = list(await observer.scalars(select(AuthSession)))
        password_hash = await observer.scalar(select(User.password_hash))
        counters = await observer.execute(
            select(AuthRateLimit.scope, AuthRateLimit.attempt_count)
        )
        counts = {scope: count for scope, count in counters.tuples()}
    assert len(sessions) == 2
    assert all(row.user_id == user_id and row.revoked_at is None for row in sessions)
    assert all(
        row.expires_at - row.created_at == timedelta(hours=12) for row in sessions
    )
    assert {(row.token_hash, row.csrf_token_hash) for row in sessions} == {
        (sha256(token).digest(), sha256(csrf).digest()) for token, csrf in pairs
    }
    assert password_hash is not None and password_hash != old_hash
    assert PasswordHasher().verify(password_hash, normalized)
    assert extract_parameters(password_hash).memory_cost == 19456
    assert counts == {"login_ip": 4, "login_email": 4}


async def test_invalid_credentials_have_one_response(
    financial_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    password = "Synthetic password marker"
    password_hash = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1).hash(
        password
    )
    async with financial_engine.begin() as connection:
        await connection.execute(
            insert(User),
            [
                {
                    "email": EMAIL,
                    "password_hash": password_hash,
                    "timezone": "UTC",
                    "is_active": True,
                },
                {
                    "email": "inactive@example.org",
                    "password_hash": password_hash,
                    "timezone": "UTC",
                    "is_active": False,
                },
            ],
        )
    async with auth_client() as client:
        responses = [
            await client.post(LOGIN, json={"email": email, "password": submitted})
            for email, submitted in (
                (EMAIL, "short"),
                ("missing@example.org", password),
                ("inactive@example.org", password),
            )
        ]
    assert all(response.status_code == 401 for response in responses)
    assert all(response.content == responses[0].content for response in responses)
    assert all("set-cookie" not in response.headers for response in responses)
    assert all(
        response.headers["cache-control"] == "no-store" for response in responses
    )
    assert responses[0].json()["error"]["code"] == "invalid_credentials"
    for private in (EMAIL, password, password_hash, SECRET.hex()):
        assert private not in responses[0].text + caplog.text
    async with AsyncSession(financial_engine) as observer:
        assert await observer.scalar(select(AuthSession.id)) is None


async def test_concurrent_registration_limit_survives_new_app(
    financial_engine: AsyncEngine,
) -> None:
    payload = {
        "email": EMAIL,
        "password": "Synthetic registration password",
        "timezone": "UTC",
    }
    async with auth_client() as first, auth_client() as second:
        clients = (first, second)
        responses = await asyncio.gather(
            *(
                clients[index % 2].post(
                    REGISTER,
                    json=payload,
                    headers={"X-Forwarded-For": f"198.51.100.{index + 1}"},
                )
                for index in range(8)
            )
        )
    assert Counter(response.status_code for response in responses) == {
        201: 1,
        409: 4,
        429: 3,
    }
    assert all("set-cookie" not in response.headers for response in responses)
    async with auth_client() as restarted:
        response = await restarted.post(REGISTER, json=payload)
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) in range(901)
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["code"] == "rate_limited"
    async with AsyncSession(financial_engine) as observer:
        rows = list(await observer.scalars(select(AuthRateLimit)))
        assert len(rows) == 1
        assert rows[0].scope == "register_ip" and rows[0].attempt_count == 5
        assert rows[0].key_digest == hmac.digest(
            SECRET, b"register_ip\0" + ip_address("192.0.2.1").packed, "sha256"
        )


async def test_login_email_limit_charges_ip_and_new_window_admits(
    financial_engine: AsyncEngine, financial_migrator: AsyncEngine
) -> None:
    async with auth_client() as first, auth_client("192.0.2.2") as second:
        clients = (first, second)
        names = (EMAIL, "  Test.Name+Work@Example.Org  ")
        responses = await asyncio.gather(
            *(
                clients[index % 2].post(
                    LOGIN, json={"email": names[index % 2], "password": "short"}
                )
                for index in range(13)
            )
        )
    assert Counter(response.status_code for response in responses) == {401: 10, 429: 3}
    async with AsyncSession(financial_engine) as observer:
        rows = list(await observer.scalars(select(AuthRateLimit)))
        assert sum(row.attempt_count for row in rows if row.scope == "login_ip") == 13
        email_row = next(row for row in rows if row.scope == "login_email")
        assert email_row.attempt_count == 10
        assert email_row.key_digest == hmac.digest(
            SECRET, b"login_email\0" + EMAIL.encode("ascii"), "sha256"
        )

    # Expire the exhausted buckets on the disposable test database.
    async with financial_migrator.begin() as connection:
        await connection.execute(
            text(
                "UPDATE auth_rate_limits SET window_start = window_start - interval '15 minutes'"
            )
        )
    async with auth_client() as restarted:
        assert (
            await restarted.post(LOGIN, json={"email": EMAIL, "password": "short"})
        ).status_code == 401
    async with AsyncSession(financial_engine) as observer:
        counts = list(
            await observer.scalars(
                select(AuthRateLimit.attempt_count).where(
                    AuthRateLimit.scope == "login_email"
                )
            )
        )
        assert sorted(counts) == [1, 10]


async def test_login_ip_limit_precedes_email_under_concurrency(
    financial_engine: AsyncEngine,
) -> None:
    key = hmac.digest(SECRET, b"login_ip\0" + ip_address("192.0.2.1").packed, "sha256")
    async with financial_engine.begin() as connection:
        await connection.execute(
            text("""
            INSERT INTO auth_rate_limits (scope, key_digest, window_start, attempt_count)
            VALUES ('login_ip', :key, date_bin(interval '15 minutes', clock_timestamp(),
                    timestamptz '1970-01-01 00:00:00+00'), 29)
        """),
            {"key": key},
        )
    async with auth_client() as first, auth_client() as second:
        responses = await asyncio.gather(
            first.post(LOGIN, json={"email": EMAIL, "password": "short"}),
            second.post(
                LOGIN, json={"email": "second@example.org", "password": "short"}
            ),
        )
    assert sorted(response.status_code for response in responses) == [401, 429]
    async with AsyncSession(financial_engine) as observer:
        rows = list(await observer.scalars(select(AuthRateLimit)))
    assert {(row.scope, row.attempt_count) for row in rows} == {
        ("login_ip", 30),
        ("login_email", 1),
    }
    assert len(rows) == 2


async def test_trusted_proxy_uses_one_valid_forwarded_ip(
    financial_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FINANCEIQ_AUTH_TRUSTED_PROXIES", '["192.0.2.1"]')
    async with auth_client() as client:
        for headers in (
            [("X-Forwarded-For", "198.51.100.1")],
            [("X-Forwarded-For", "::ffff:198.51.100.1")],
            [("X-Forwarded-For", "198.51.100.2"), ("X-Forwarded-For", "198.51.100.3")],
            [("X-Forwarded-For", "198.51.100.2, 198.51.100.3")],
        ):
            response = await client.post(
                LOGIN, json={"email": EMAIL, "password": "short"}, headers=headers
            )
            assert response.status_code == 401
    async with AsyncSession(financial_engine) as observer:
        rows = list(
            await observer.scalars(
                select(AuthRateLimit).where(AuthRateLimit.scope == "login_ip")
            )
        )
    assert {row.key_digest: row.attempt_count for row in rows} == {
        hmac.digest(SECRET, b"login_ip\0" + ip_address(ip).packed, "sha256"): 2
        for ip in ("192.0.2.1", "198.51.100.1")
    }


async def test_unavailable_limits_fail_before_argon(
    financial_engine: AsyncEngine,
    financial_migrator: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verify = AsyncMock()
    hash_password = AsyncMock()
    monkeypatch.setattr(service, "verify_password", verify)
    monkeypatch.setattr(service, "hash_password", hash_password)
    async with financial_migrator.begin() as connection:
        await connection.execute(
            text("REVOKE INSERT ON auth_rate_limits FROM financeiq_runtime")
        )
    try:
        async with auth_client() as client:
            payload = {"email": EMAIL, "password": "Synthetic password before failure"}
            for path, body in (
                (LOGIN, payload),
                (REGISTER, payload | {"timezone": "UTC"}),
            ):
                response = await client.post(path, json=body)
                assert response.status_code == 500
                assert response.json()["error"]["code"] == "internal_error"
                assert "set-cookie" not in response.headers
    finally:
        async with financial_migrator.begin() as connection:
            await connection.execute(
                text("GRANT INSERT ON auth_rate_limits TO financeiq_runtime")
            )
    verify.assert_not_awaited()
    hash_password.assert_not_awaited()
    async with AsyncSession(financial_engine) as observer:
        assert await observer.scalar(select(AuthRateLimit.scope)) is None


async def test_commit_failure_rolls_back_session_and_rehash_but_keeps_admission(
    financial_engine: AsyncEngine,
    financial_migrator: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    password = "Synthetic password before commit"
    old_hash = PasswordHasher(memory_cost=8192, time_cost=2, parallelism=1).hash(
        password
    )
    async with financial_engine.begin() as connection:
        await connection.execute(
            insert(User).values(email=EMAIL, password_hash=old_hash, timezone="UTC")
        )
    async with financial_migrator.begin() as connection:
        await connection.execute(
            text("""
            CREATE FUNCTION auth_test_commit_failure() RETURNS trigger LANGUAGE plpgsql
            AS $$ BEGIN RAISE EXCEPTION 'private-commit-failure'; END $$
        """)
        )
        await connection.execute(
            text("""
            CREATE CONSTRAINT TRIGGER auth_test_commit_failure AFTER INSERT ON auth_sessions
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION auth_test_commit_failure()
        """)
        )
    try:
        async with auth_client() as client:
            response = await client.post(
                LOGIN, json={"email": EMAIL, "password": password}
            )
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal_error"
        assert response.headers["cache-control"] == "no-store"
        assert "set-cookie" not in response.headers
        for private in (EMAIL, password, old_hash, "private-commit-failure"):
            assert private not in response.text + caplog.text
    finally:
        async with financial_migrator.begin() as connection:
            await connection.execute(
                text("DROP TRIGGER auth_test_commit_failure ON auth_sessions")
            )
            await connection.execute(text("DROP FUNCTION auth_test_commit_failure()"))
    async with AsyncSession(financial_engine) as observer:
        assert await observer.scalar(select(AuthSession.id)) is None
        assert await observer.scalar(select(User.password_hash)) == old_hash
        assert sorted(await observer.scalars(select(AuthRateLimit.attempt_count))) == [
            1,
            1,
        ]
