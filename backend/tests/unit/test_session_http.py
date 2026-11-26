"""Public contracts for protected Auth routes without a database."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from financeiq.main import create_app

ME = "/api/v1/auth/me"
LOGOUT = "/api/v1/auth/logout"
COOKIE = "__Host-financeiq-session"


@pytest.fixture(autouse=True)
def auth_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCEIQ_APP_ENV", "test")
    monkeypatch.setenv(
        "FINANCEIQ_DATABASE_URL",
        "postgresql+asyncpg://test:unused@127.0.0.1:5432/financeiq_test",
    )


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(), base_url="https://test") as test_client:
        yield test_client


@pytest.mark.parametrize(("method", "path"), [("GET", ME), ("POST", LOGOUT)])
@pytest.mark.parametrize(
    "token", [None, "short", "A" * 42 + "+", "A" * 42 + "B", "A" * 43 + "="]
)
def test_missing_or_noncanonical_cookie_is_unauthenticated(
    client: TestClient, method: str, path: str, token: str | None
) -> None:
    headers = {} if token is None else {"Cookie": f"{COOKIE}={token}"}
    response = client.request(method, path, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


def test_openapi_describes_protected_routes(client: TestClient) -> None:
    schema = client.get("/api/v1/openapi.json").json()
    me = schema["paths"][ME]["get"]
    logout = schema["paths"][LOGOUT]["post"]
    assert me["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/UserProfile"
    }
    assert "requestBody" not in logout
    assert "content" not in logout["responses"]["204"]
    assert "Set-Cookie" in logout["responses"]["204"]["headers"]
    for operation in (me, logout):
        scheme = next(iter(operation["security"][0]))
        assert schema["components"]["securitySchemes"][scheme] == {
            "type": "apiKey",
            "in": "cookie",
            "name": COOKIE,
        }
        assert set(operation["responses"]) >= {"401", "500"}


def test_production_does_not_expose_session_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FINANCEIQ_APP_ENV", "production")
    with TestClient(create_app()) as production:
        for response in (production.get(ME), production.post(LOGOUT)):
            assert response.status_code == 404
            assert response.headers["cache-control"] == "no-store"
