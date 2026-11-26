"""Registration HTTP contract without a PostgreSQL connection."""

import json
import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from financeiq.main import create_app

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
VALID = {
    "email": "private-auth@example.org",
    "password": "Private password marker",
    "timezone": "Europe/Saratov",
}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FINANCEIQ_APP_ENV", "test")
    monkeypatch.delenv("FINANCEIQ_AUTH_RATE_LIMIT_SECRET", raising=False)
    monkeypatch.setenv(
        "FINANCEIQ_DATABASE_URL",
        "postgresql+asyncpg://test:unused@127.0.0.1:5432/financeiq_test",
    )
    with TestClient(create_app()) as test_client:
        yield test_client


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
def test_production_has_no_auth_commands(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setenv("FINANCEIQ_APP_ENV", "production")
    monkeypatch.setenv(
        "FINANCEIQ_DATABASE_URL",
        "postgresql+asyncpg://test:unused@127.0.0.1:5432/financeiq_test",
    )
    with TestClient(create_app()) as client:
        response = client.post(path, json=VALID)
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["code"] == "not_found"


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
