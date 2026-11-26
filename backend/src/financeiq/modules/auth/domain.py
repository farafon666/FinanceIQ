"""Validation shared by Auth commands."""

from functools import cache
from importlib.resources import files
from unicodedata import normalize

from email_validator import EmailNotValidError, validate_email


def normalize_email(value: str) -> str:
    """Keep provider-specific dots and suffixes while canonicalizing ASCII identity."""
    candidate = value.strip()
    if not candidate or any(not "!" <= character <= "~" for character in candidate):
        raise ValueError("Email must contain printable ASCII without whitespace.")
    return candidate.lower()


def validate_email_address(value: str) -> str:
    candidate = normalize_email(value)
    try:
        validate_email(candidate, check_deliverability=False, allow_smtputf8=False)
    except EmailNotValidError:
        raise ValueError("Invalid email syntax.") from None
    return candidate


def normalize_password(value: str) -> str:
    return normalize("NFC", value)


@cache
def _iana_zones() -> frozenset[str]:
    return frozenset(files("tzdata").joinpath("zones").read_text("utf-8").splitlines())


def validate_timezone(value: str) -> str:
    if value not in _iana_zones():
        raise ValueError("Unknown IANA time zone.")
    return value
