"""Exercise Auth commands against PostgreSQL as the runtime role."""

import asyncio
import hmac
from base64 import urlsafe_b64decode
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from copy import copy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from http.cookies import SimpleCookie
from ipaddress import ip_address
from unicodedata import normalize
from unittest.mock import AsyncMock

import httpx
import pytest
from argon2 import PasswordHasher, extract_parameters
from sqlalchemy import DateTime, func, insert, literal, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from financeiq.main import create_app
from financeiq.modules.auth import passwords, repository, service
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
ME = "/api/v1/auth/me"
LOGOUT = "/api/v1/auth/logout"
ORIGIN = "https://test"
SESSION_COOKIE = "__Host-financeiq-session"
CSRF_COOKIE = "__Host-financeiq-csrf"


@pytest.fixture
def auth_environment(
    runtime_postgres: TestPostgres, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FINANCEIQ_APP_ENV", "test")
    monkeypatch.setenv("FINANCEIQ_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setenv(
        "FINANCEIQ_DATABASE_URL",
        runtime_postgres.url("financeiq_runtime", sqlalchemy=True),
    )
    monkeypatch.setenv("FINANCEIQ_AUTH_RATE_LIMIT_SECRET", SECRET.hex())
    monkeypatch.delenv("FINANCEIQ_AUTH_TRUSTED_PROXIES", raising=False)
    # Exercise sessions and counters before physical removal, covered separately.
    monkeypatch.setattr("financeiq.main.run_auth_cleanup", AsyncMock())


@pytest.fixture
def freeze_database_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[datetime], None]:
    auth_functions = copy(func)
    monkeypatch.setattr(repository, "func", auth_functions)

    def freeze(instant: datetime) -> None:
        # Keep the comparisons and window arithmetic in PostgreSQL.
        monkeypatch.setattr(
            auth_functions,
            "clock_timestamp",
            lambda **_: literal(instant, type_=DateTime(timezone=True)),
            raising=False,
        )

    return freeze


@asynccontextmanager
async def auth_client(ip: str = "192.0.2.1") -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(ip, 12345)),
            base_url=ORIGIN,
            headers={"Origin": ORIGIN},
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
    password = " Pre\u0301serve" + "z" * 118 + " "
    normalized = normalize("NFC", password)
    assert len(normalized) == 128
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
        for wrong_password in (
            normalized.strip(),
            normalized.lower(),
            normalized[:-1] + "x",
        ):
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
    assert counts == {"login_ip": 5, "login_email": 5}


async def test_invalid_credentials_have_one_response(
    financial_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = "Synthetic password marker"
    password_hash = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1).hash(
        password
    )
    verified_hashes: list[str | bytes] = []
    verify = PasswordHasher.verify

    def observe_verify(
        hasher: PasswordHasher, encoded: str | bytes, candidate: str | bytes
    ) -> bool:
        verified_hashes.append(encoded)
        return verify(hasher, encoded, candidate)

    monkeypatch.setattr(PasswordHasher, "verify", observe_verify)
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
    with caplog.at_level("DEBUG"):
        async with auth_client() as client:
            responses = [
                await client.post(LOGIN, json={"email": email, "password": submitted})
                for email, submitted in (
                    (EMAIL, "é" * 14),
                    ("missing@example.org", password),
                    ("inactive@example.org", password),
                )
            ]
    assert verified_hashes == [
        password_hash,
        passwords.DUMMY_PASSWORD_HASH,
        passwords.DUMMY_PASSWORD_HASH,
    ]
    assert all(response.status_code == 401 for response in responses)
    assert all(response.content == responses[0].content for response in responses)
    assert all("set-cookie" not in response.headers for response in responses)
    assert all(
        response.headers["cache-control"] == "no-store" for response in responses
    )
    assert responses[0].json()["error"]["code"] == "invalid_credentials"
    for private in (
        EMAIL,
        "missing@example.org",
        "inactive@example.org",
        "é" * 14,
        password,
        password_hash,
        passwords.DUMMY_PASSWORD_HASH,
        SECRET.hex(),
    ):
        assert private not in responses[0].text + caplog.text
    async with AsyncSession(financial_engine) as observer:
        assert await observer.scalar(select(AuthSession.id)) is None


async def test_rejected_origin_does_not_create_records_or_reserve_attempts(
    financial_engine: AsyncEngine,
) -> None:
    credentials = {"email": EMAIL, "password": "Synthetic password for origin tests"}
    registration = credentials | {"timezone": "UTC"}
    async with auth_client() as client:
        response = await client.post(REGISTER, json=registration)
        assert response.status_code == 201
        for path, payload in (
            (REGISTER, registration | {"email": "other@example.org"}),
            (LOGIN, credentials),
        ):
            response = await client.post(path, json=payload, headers={"Origin": "null"})
            assert response.status_code == 403
            assert "set-cookie" not in response.headers

    async with AsyncSession(financial_engine) as observer:
        assert list(await observer.scalars(select(User.email))) == [EMAIL]
        assert await observer.scalar(select(AuthSession.id)) is None
        counters = await observer.execute(
            select(AuthRateLimit.scope, AuthRateLimit.attempt_count)
        )
        assert counters.tuples().all() == [("register_ip", 1)]


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
    financial_engine: AsyncEngine, freeze_database_clock: Callable[[datetime], None]
) -> None:
    window_end = datetime(2026, 11, 27, 12, 15, tzinfo=UTC)
    freeze_database_clock(window_end - timedelta(minutes=10))
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
    for response in responses:
        if response.status_code == 429:
            assert response.headers["retry-after"] == "600"
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["error"]["code"] == "rate_limited"
    async with AsyncSession(financial_engine) as observer:
        rows = list(await observer.scalars(select(AuthRateLimit)))
        assert sum(row.attempt_count for row in rows if row.scope == "login_ip") == 13
        email_row = next(row for row in rows if row.scope == "login_email")
        assert email_row.attempt_count == 10
        assert email_row.key_digest == hmac.digest(
            SECRET, b"login_email\0" + EMAIL.encode("ascii"), "sha256"
        )

    credentials = {"email": EMAIL, "password": "short"}
    freeze_database_clock(window_end - timedelta(microseconds=1))
    async with auth_client() as restarted:
        response = await restarted.post(LOGIN, json=credentials)
        assert response.status_code == 429
        assert response.headers["retry-after"] == "1"

        freeze_database_clock(window_end)
        response = await restarted.post(LOGIN, json=credentials)
        assert response.status_code == 401
        assert "retry-after" not in response.headers
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


async def register_and_login(
    client: httpx.AsyncClient, email: str = EMAIL
) -> dict[str, str]:
    credentials = {"email": email, "password": "Synthetic password for session tests"}
    registered = await client.post(REGISTER, json=credentials | {"timezone": "UTC"})
    assert registered.status_code == 201
    session_secrets(await client.post(LOGIN, json=credentials))
    profile: dict[str, str] = registered.json()
    return profile


def assert_authentication_required(response: httpx.Response) -> None:
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


async def test_session_profile_and_concurrent_logout_preserve_other_session(
    financial_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with auth_client() as first, auth_client() as second:
        expected_profile = await register_and_login(first)
        first_token = first.cookies[SESSION_COOKIE]
        session_secrets(
            await second.post(
                LOGIN,
                json={
                    "email": EMAIL,
                    "password": "Synthetic password for session tests",
                },
            )
        )
        assert second.cookies[SESSION_COOKIE] != first_token
        first_csrf = first.cookies[CSRF_COOKIE]
        second_csrf = second.cookies[CSRF_COOKIE]
        for invalid_headers in (
            [],
            [("X-CSRF-Token", "private-token-marker")],
            [("X-CSRF-Token", second_csrf)],
            [("X-CSRF-Token", first_csrf), ("X-CSRF-Token", first_csrf)],
            [("X-CSRF-Token", first_csrf), ("Origin", "https://other.test")],
        ):
            response = await first.post(LOGOUT, headers=invalid_headers)
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "forbidden"
            assert response.headers["cache-control"] == "no-store"
            assert "set-cookie" not in response.headers
            for private in (
                first_token,
                first_csrf,
                second_csrf,
                "private-token-marker",
            ):
                assert private not in response.text + caplog.text
        assert (await first.get(ME)).json() == expected_profile
        assert (await second.get(ME)).json() == expected_profile
        async with financial_engine.connect() as observer:
            times = select(AuthSession.id, AuthSession.expires_at).order_by(
                AuthSession.id
            )
            before = (await observer.execute(times)).all()
            profile = await first.get(ME, params={"id": "untrusted-user-id"})
            assert profile.status_code == 200
            assert profile.json() == expected_profile
            assert profile.headers["cache-control"] == "no-store"
            assert "set-cookie" not in profile.headers
            assert (await observer.execute(times)).all() == before

        headers = {
            "Cookie": f"{SESSION_COOKIE}={first_token}",
            "X-CSRF-Token": first_csrf,
        }
        first_result, second_result = await asyncio.gather(
            first.post(LOGOUT, headers=headers),
            first.post(LOGOUT, headers=headers),
        )
        success, refused = sorted(
            (first_result, second_result), key=lambda response: response.status_code
        )
        assert success.status_code == 204 and success.content == b""
        assert success.headers["cache-control"] == "no-store"
        cookie_headers = success.headers.get_list("set-cookie")
        assert len(cookie_headers) == 2
        cleared = SimpleCookie()
        for header in cookie_headers:
            cleared.load(header)
        assert set(cleared) == {SESSION_COOKIE, CSRF_COOKIE}
        for name, cookie in cleared.items():
            assert cookie.value == ""
            assert cookie["max-age"] == "0"
            assert cookie["secure"] and not cookie["domain"]
            assert cookie["path"] == "/" and cookie["samesite"].lower() == "strict"
            assert bool(cookie["httponly"]) == (name == SESSION_COOKIE)
            assert name not in first.cookies
        assert_authentication_required(refused)
        assert_authentication_required(
            await first.get(ME, headers={"Cookie": headers["Cookie"]})
        )
        assert (await second.get(ME)).json() == expected_profile

    async with financial_engine.connect() as observer:
        count = await observer.scalar(
            text("SELECT count(*) FROM auth_sessions WHERE revoked_at IS NOT NULL")
        )
        assert count == 1


@pytest.mark.parametrize(
    "state",
    [
        "UPDATE auth_sessions SET expires_at = clock_timestamp()",
        "UPDATE auth_sessions SET revoked_at = clock_timestamp()",
        "UPDATE users SET is_active = false",
    ],
    ids=["expired", "revoked", "inactive"],
)
async def test_stored_invalid_session_fails_before_cleanup(
    financial_engine: AsyncEngine, state: str
) -> None:
    async with auth_client() as client:
        await register_and_login(client)
        async with financial_engine.begin() as connection:
            await connection.execute(text(state))
        assert_authentication_required(await client.get(ME))
        assert_authentication_required(await client.post(LOGOUT))
        assert_authentication_required(
            await client.patch(
                ME,
                json={"timezone": "Europe/Saratov"},
                headers={"X-CSRF-Token": client.cookies[CSRF_COOKIE]},
            )
        )
    async with financial_engine.connect() as connection:
        assert await connection.scalar(select(AuthSession.id)) is not None
        assert await connection.scalar(select(User.timezone)) == "UTC"


async def test_unknown_session_is_unauthenticated() -> None:
    async with auth_client() as client:
        assert_authentication_required(
            await client.get(ME, headers={"Cookie": f"{SESSION_COOKIE}={'A' * 43}"})
        )


async def test_session_expires_at_the_exact_boundary(
    financial_engine: AsyncEngine, freeze_database_clock: Callable[[datetime], None]
) -> None:
    async with auth_client() as client:
        profile = await register_and_login(client)
        async with financial_engine.connect() as observer:
            expires_at = await observer.scalar(select(AuthSession.expires_at))
        assert expires_at is not None

        freeze_database_clock(expires_at - timedelta(microseconds=1))
        response = await client.get(ME)
        assert response.status_code == 200
        assert response.json() == profile

        freeze_database_clock(expires_at)
        assert_authentication_required(await client.get(ME))


async def test_profile_is_scoped_to_the_session_owner(
    financial_engine: AsyncEngine,
) -> None:
    async with auth_client() as first, auth_client() as second:
        first_profile = await register_and_login(first)
        second_profile = await register_and_login(second, "other@example.org")
        assert (await second.get(ME, params={"id": first_profile["id"]})).json() == (
            second_profile
        )


async def test_timezone_update_is_visible_to_the_owners_other_session(
    financial_engine: AsyncEngine,
) -> None:
    async with auth_client() as first, auth_client() as second, auth_client() as other:
        profile = await register_and_login(first)
        session_secrets(
            await second.post(
                LOGIN,
                json={
                    "email": EMAIL,
                    "password": "Synthetic password for session tests",
                },
            )
        )
        other_profile = await register_and_login(other, "other@example.org")
        async with financial_engine.connect() as observer:
            sessions = select(AuthSession.id, AuthSession.expires_at).order_by(
                AuthSession.id
            )
            before = (await observer.execute(sessions)).all()
            response = await first.patch(
                ME,
                json={"timezone": "Europe/Saratov"},
                headers={"X-CSRF-Token": first.cookies[CSRF_COOKIE]},
            )
            assert response.status_code == 200
            updated = profile | {"timezone": "Europe/Saratov"}
            assert response.json() == updated
            assert response.headers["cache-control"] == "no-store"
            assert "set-cookie" not in response.headers
            assert (await second.get(ME)).json() == updated
            assert (await other.get(ME)).json() == other_profile
            assert (await observer.execute(sessions)).all() == before


@pytest.mark.usefixtures("financial_engine")
async def test_timezone_update_rejects_csrf_and_invalid_fields(
    subtests: pytest.Subtests,
) -> None:
    async with auth_client() as client, auth_client() as other:
        profile = await register_and_login(client)
        other_profile = await register_and_login(other, "other@example.org")
        csrf = client.cookies[CSRF_COOKIE]
        valid = {"timezone": "Europe/Saratov"}
        for headers in ({}, {"X-CSRF-Token": other.cookies[CSRF_COOKIE]}):
            response = await client.patch(ME, json=valid, headers=headers)
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "forbidden"
            assert response.headers["cache-control"] == "no-store"

        for payload, path, code in (
            ({}, ["timezone"], "required"),
            ({"timezone": "Unknown/Private_Zone"}, ["timezone"], "invalid_value"),
            ({"timezone": " Europe/Saratov "}, ["timezone"], "invalid_value"),
            ({"timezone": 123}, ["timezone"], "invalid_type"),
            ({"timezone": None}, ["timezone"], "invalid_type"),
            (valid | {"id": other_profile["id"]}, [], "unknown_field"),
            (valid | {"email": other_profile["email"]}, [], "unknown_field"),
        ):
            with subtests.test(payload=payload):
                response = await client.patch(
                    ME, json=payload, headers={"X-CSRF-Token": csrf}
                )
                assert response.status_code == 422
                assert response.headers["cache-control"] == "no-store"
                error = response.json()["error"]
                assert error["code"] == "validation_error"
                assert error["fields"] == [
                    {"location": "body", "path": path, "code": code}
                ]
                assert "Unknown/Private_Zone" not in response.text
        assert (await client.get(ME)).json() == profile
        assert (await other.get(ME)).json() == other_profile


@pytest.mark.usefixtures("financial_engine")
async def test_timezone_commit_failure_preserves_saved_profile(
    financial_migrator: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with auth_client() as client:
        profile = await register_and_login(client)
        async with financial_migrator.begin() as connection:
            await connection.execute(
                text("""
                CREATE FUNCTION auth_test_timezone_failure() RETURNS trigger LANGUAGE plpgsql
                AS $$ BEGIN RAISE EXCEPTION 'private-timezone-failure'; END $$
                """)
            )
            await connection.execute(
                text("""
                CREATE CONSTRAINT TRIGGER auth_test_timezone_failure AFTER UPDATE ON users
                DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION auth_test_timezone_failure()
                """)
            )
        try:
            response = await client.patch(
                ME,
                json={"timezone": "Europe/Saratov"},
                headers={"X-CSRF-Token": client.cookies[CSRF_COOKIE]},
            )
            assert response.status_code == 500
            assert response.json()["error"]["code"] == "internal_error"
            assert response.headers["cache-control"] == "no-store"
            assert "set-cookie" not in response.headers
            assert "private-timezone-failure" not in response.text + caplog.text
            assert (await client.get(ME)).json() == profile
        finally:
            async with financial_migrator.begin() as connection:
                await connection.execute(
                    text("DROP TRIGGER auth_test_timezone_failure ON users")
                )
                await connection.execute(
                    text("DROP FUNCTION auth_test_timezone_failure()")
                )


async def test_logout_checks_expiry_after_waiting_for_session_lock(
    financial_engine: AsyncEngine,
) -> None:
    async with auth_client() as client, financial_engine.connect() as holder:
        await register_and_login(client)
        async with holder.begin():
            await holder.execute(
                text(
                    "UPDATE auth_sessions SET expires_at = clock_timestamp() + interval '3 seconds'"
                )
            )
        task = None
        try:
            async with holder.begin(), asyncio.timeout(5):
                holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                await holder.execute(text("SELECT id FROM auth_sessions FOR UPDATE"))
                task = asyncio.create_task(
                    client.post(
                        LOGOUT, headers={"X-CSRF-Token": client.cookies[CSRF_COOKIE]}
                    )
                )
                while not await holder.scalar(
                    text("""
                    SELECT EXISTS (
                        SELECT FROM pg_stat_activity
                        WHERE :holder = ANY(pg_blocking_pids(pid))
                    )
                    """),
                    {"holder": holder_pid},
                ):
                    assert not task.done()
                    await asyncio.sleep(0.01)
                await holder.execute(
                    text("SELECT pg_sleep_until(expires_at) FROM auth_sessions")
                )
            assert_authentication_required(await asyncio.wait_for(task, timeout=5))
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert await holder.scalar(select(AuthSession.revoked_at)) is None


async def test_logout_commit_failure_preserves_session_and_browser_cookies(
    financial_engine: AsyncEngine,
    financial_migrator: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with auth_client() as client:
        profile = await register_and_login(client)
        token = client.cookies[SESSION_COOKIE]
        async with financial_migrator.begin() as connection:
            await connection.execute(
                text("""
                CREATE FUNCTION auth_test_logout_failure() RETURNS trigger LANGUAGE plpgsql
                AS $$ BEGIN RAISE EXCEPTION 'private-logout-failure'; END $$
                """)
            )
            await connection.execute(
                text("""
                CREATE CONSTRAINT TRIGGER auth_test_logout_failure AFTER UPDATE ON auth_sessions
                DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION auth_test_logout_failure()
                """)
            )
        try:
            response = await client.post(
                LOGOUT, headers={"X-CSRF-Token": client.cookies[CSRF_COOKIE]}
            )
            assert response.status_code == 500
            assert response.json()["error"]["code"] == "internal_error"
            assert response.headers["cache-control"] == "no-store"
            assert "set-cookie" not in response.headers
            assert client.cookies[SESSION_COOKIE] == token
            assert (await client.get(ME)).json() == profile
            for private in (token, EMAIL, "private-logout-failure"):
                assert private not in response.text + caplog.text
        finally:
            async with financial_migrator.begin() as connection:
                await connection.execute(
                    text("DROP TRIGGER auth_test_logout_failure ON auth_sessions")
                )
                await connection.execute(
                    text("DROP FUNCTION auth_test_logout_failure()")
                )
    async with financial_engine.connect() as connection:
        row = (await connection.execute(select(AuthSession.revoked_at))).one()
        assert row.revoked_at is None
