"""Registration HTTP contract without a PostgreSQL connection."""

import json
import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from httpx import SyncByteStream

from financeiq.main import create_app

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
ORIGIN = "https://financeiq.test:8443"
VALID = {
    "email": "private-auth@example.org",
    "password": "Private password marker",
    "timezone": "Europe/Saratov",
}


class UnreadableBody(SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        raise AssertionError("The rejected request body must stay unread.")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FINANCEIQ_APP_ENV", "test")
    monkeypatch.setenv("FINANCEIQ_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.delenv("FINANCEIQ_AUTH_RATE_LIMIT_SECRET", raising=False)
    monkeypatch.setenv(
        "FINANCEIQ_DATABASE_URL",
        "postgresql+asyncpg://test:unused@127.0.0.1:5432/financeiq_test",
    )
    with TestClient(
        create_app(), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", REGISTER), ("POST", LOGIN), ("PATCH", "/api/v1/auth/me")],
)
@pytest.mark.parametrize(
    "origins",
    [
        [],
        ["null"],
        ["http://financeiq.test:8443"],
        ["https://financeiq.test"],
        ["https://other.test:8443"],
        ["https://financeiq.test:8443/"],
        [ORIGIN, ORIGIN],
        [f"{ORIGIN}, {ORIGIN}"],
    ],
)
def test_invalid_origin_is_rejected_before_reading_body(
    client: TestClient, method: str, path: str, origins: list[str]
) -> None:
    request = client.build_request(
        method,
        path,
        content=UnreadableBody(),
        headers={
            "Content-Type": "application/json",
            "X-Forwarded-Host": "financeiq.test",
        },
    )
    del request.headers["Origin"]
    request.headers.update([("Origin", value) for value in origins])
    response = client.send(request)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    ("content", "content_type", "status", "code"),
    [
        ("{ private-json-marker", "application/json", 400, "invalid_json"),
        (b'"\xff"', "application/json", 400, "invalid_json"),
        ("private-json-marker", "text/plain", 415, "unsupported_media_type"),
        ("{}", "application/vnd.example+json", 415, "unsupported_media_type"),
        ("{}", None, 415, "unsupported_media_type"),
        (b"", None, 422, "validation_error"),
    ],
)
def test_media_type_and_json_errors(
    client: TestClient,
    content: str | bytes,
    content_type: str | None,
    status: int,
    code: str,
) -> None:
    headers = {"Content-Type": content_type} if content_type else {}
    response = client.post(REGISTER, content=content, headers=headers)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert "private-json-marker" not in response.text


@pytest.mark.parametrize(
    ("data", "path", "code"),
    [
        (
            {key: value for key, value in VALID.items() if key != "timezone"},
            ["timezone"],
            "required",
        ),
        (VALID | {"private-key-marker": "private-value-marker"}, [], "unknown_field"),
        (VALID | {"email": '"test"@example.org'}, ["email"], "invalid_value"),
        (VALID | {"email": "test@[192.0.2.1]"}, ["email"], "invalid_value"),
        (VALID | {"email": "Test <test@example.org>"}, ["email"], "invalid_value"),
        (VALID | {"email": "test(comment)@example.org"}, ["email"], "invalid_value"),
        (VALID | {"timezone": "Unknown/Private_Zone"}, ["timezone"], "invalid_value"),
        (VALID | {"timezone": " Europe/Saratov "}, ["timezone"], "invalid_value"),
        (VALID | {"password": 123456789012345}, ["password"], "invalid_type"),
        (VALID | {"password": None}, ["password"], "invalid_type"),
        (VALID | {"password": "e\u0301" * 14}, ["password"], "invalid_value"),
        (VALID | {"password": "e\u0301" * 129}, ["password"], "invalid_value"),
        (VALID | {"password": "x" * 15 + "\ud800"}, ["password"], "invalid_value"),
        ([], [], "invalid_type"),
    ],
)
def test_invalid_input_uses_safe_fields(
    client: TestClient,
    data: object,
    path: list[str],
    code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = client.post(
            REGISTER,
            content=json.dumps(data),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "error": {
            "code": "validation_error",
            "message": "Проверьте параметры запроса.",
            "fields": [{"location": "body", "path": path, "code": code}],
        }
    }
    for private in (*VALID.values(), "private-key-marker", "private-value-marker"):
        assert private not in response.text + caplog.text


def test_registration_route_errors_are_not_cached(client: TestClient) -> None:
    response = client.get(REGISTER)
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["code"] == "method_not_allowed"


@pytest.mark.parametrize("path", [REGISTER, LOGIN])
def test_production_checks_origin_on_auth_commands(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setenv("FINANCEIQ_APP_ENV", "production")
    monkeypatch.setenv("FINANCEIQ_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setenv("FINANCEIQ_AUTH_RATE_LIMIT_SECRET", "ab" * 32)
    monkeypatch.setenv(
        "FINANCEIQ_DATABASE_URL",
        "postgresql+asyncpg://test:unused@127.0.0.1:5432/financeiq_test",
    )
    with TestClient(create_app()) as client:
        response = client.post(path, json=VALID)
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["code"] == "forbidden"


def test_login_validation_and_missing_secret(client: TestClient) -> None:
    assert (
        client.post(
            LOGIN, content="{}", headers={"Content-Type": "text/plain"}
        ).status_code
        == 415
    )
    credentials = {"email": VALID["email"], "password": "e\u0301" * 129}
    response = client.post(LOGIN, json=credentials)
    assert response.status_code == 422
    assert response.json()["error"]["fields"] == [
        {"location": "body", "path": ["password"], "code": "invalid_value"}
    ]
    response = client.post(LOGIN, json=credentials | {"password": "short"})
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert client.get("/api/v1/health").status_code == 200
