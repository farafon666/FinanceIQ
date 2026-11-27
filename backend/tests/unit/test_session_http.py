"""Public contracts for protected Auth routes without a database."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from financeiq.main import create_app

ME = "/api/v1/auth/me"
LOGOUT = "/api/v1/auth/logout"
COOKIE = "__Host-financeiq-session"
ORIGIN = "https://test"


@pytest.fixture(autouse=True)
def auth_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCEIQ_APP_ENV", "test")
    monkeypatch.setenv("FINANCEIQ_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setenv(
        "FINANCEIQ_DATABASE_URL",
        "postgresql+asyncpg://test:unused@127.0.0.1:5432/financeiq_test",
    )


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(), base_url=ORIGIN) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("method", "path"), [("GET", ME), ("PATCH", ME), ("POST", LOGOUT)]
)
@pytest.mark.parametrize(
    "token", [None, "short", "A" * 42 + "+", "A" * 42 + "B", "A" * 43 + "="]
)
def test_missing_or_noncanonical_cookie_is_unauthenticated(
    client: TestClient, method: str, path: str, token: str | None
) -> None:
    headers = {} if token is None else {"Cookie": f"{COOKIE}={token}"}
    if method != "GET":
        headers["Origin"] = ORIGIN
    payload = {"timezone": "UTC"} if method == "PATCH" else None
    response = client.request(method, path, headers=headers, json=payload)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    ("content_type", "status", "code"),
    [
        ("application/json", 400, "invalid_json"),
        ("text/plain", 415, "unsupported_media_type"),
        (None, 415, "unsupported_media_type"),
    ],
)
def test_patch_timezone_rejects_invalid_json_or_media_type(
    client: TestClient, content_type: str | None, status: int, code: str
) -> None:
    headers = {"Origin": ORIGIN}
    if content_type is not None:
        headers["Content-Type"] = content_type
    response = client.patch(ME, content="{ private-json-marker", headers=headers)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert "private-json-marker" not in response.text


def test_openapi_describes_protected_routes(client: TestClient) -> None:
    schema = client.get("/api/v1/openapi.json").json()
    me = schema["paths"][ME]["get"]
    change_timezone = schema["paths"][ME]["patch"]
    logout = schema["paths"][LOGOUT]["post"]
    assert me["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/UserProfile"
    }
    assert "requestBody" not in logout
    assert "content" not in logout["responses"]["204"]
    assert "Set-Cookie" in logout["responses"]["204"]["headers"]
    body = change_timezone["requestBody"]
    assert body["required"] is True
    body_schema = body["content"]["application/json"]["schema"]["$ref"].split("/")[-1]
    fields = schema["components"]["schemas"][body_schema]
    assert fields["required"] == ["timezone"]
    assert set(fields["properties"]) == {"timezone"}
    assert fields["properties"]["timezone"]["type"] == "string"
    assert fields["additionalProperties"] is False
    assert set(change_timezone["responses"]) == {
        "200",
        "400",
        "401",
        "403",
        "415",
        "422",
        "500",
    }
    assert (
        change_timezone["responses"]["200"]["content"]
        == me["responses"]["200"]["content"]
    )
    assert change_timezone["responses"]["422"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/ValidationErrorResponse"}
    for operation in (change_timezone, logout):
        assert operation["responses"]["403"]["content"]["application/json"][
            "schema"
        ] == {"$ref": "#/components/schemas/ErrorResponse"}
        assert operation["parameters"] == [
            {
                "name": "X-CSRF-Token",
                "in": "header",
                "required": True,
                "schema": {"type": "string"},
            }
        ]
    for operation in (me, change_timezone, logout):
        scheme = next(iter(operation["security"][0]))
        assert schema["components"]["securitySchemes"][scheme] == {
            "type": "apiKey",
            "in": "cookie",
            "name": COOKIE,
        }
        assert set(operation["responses"]) >= {"401", "500"}


def test_production_exposes_protected_session_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FINANCEIQ_APP_ENV", "production")
    monkeypatch.setenv("FINANCEIQ_AUTH_RATE_LIMIT_SECRET", "ab" * 32)
    with TestClient(create_app()) as production:
        assert production.post(LOGOUT).status_code == 403
        assert production.patch(ME, json={"timezone": "UTC"}).status_code == 403
        for response in (
            production.get(ME),
            production.patch(ME, headers={"Origin": ORIGIN}, json={"timezone": "UTC"}),
            production.post(LOGOUT, headers={"Origin": ORIGIN}),
        ):
            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"
