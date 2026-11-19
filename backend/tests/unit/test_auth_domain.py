"""Canonical email identity, independently of future HTTP validation."""

import pytest

from financeiq.modules.auth.domain import normalize_email


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  Alice.Example+Work@EXAMPLE.COM  ", "alice.example+work@example.com"),
        ("\tUser@Example.com\r\n", "user@example.com"),
        ("\u00a0User@Example.com\u00a0", "user@example.com"),
        ("user@xn--e1afmkfd.xn--p1ai", "user@xn--e1afmkfd.xn--p1ai"),
    ],
)
def test_email_identity(value: str, expected: str) -> None:
    assert normalize_email(value) == expected
    assert normalize_email(expected) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        " \t\n",
        "user name@example.com",
        "user\tname@example.com",
        "user\nname@example.com",
        "user\x00@example.com",
        "user\x7f@example.com",
        "пользователь@example.com",
        "user@пример.рф",
        "\u212a@example.com",
    ],
)
def test_invalid_email_identity_is_rejected_without_echoing_input(value: str) -> None:
    with pytest.raises(ValueError) as caught:
        normalize_email(value)
    assert str(caught.value) == "Email must contain printable ASCII without whitespace."
