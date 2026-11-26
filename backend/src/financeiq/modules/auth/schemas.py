"""Auth request and profile contracts."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

from financeiq.modules.auth.domain import (
    normalize_password,
    validate_email_address,
    validate_timezone,
)


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    email: str
    password: SecretStr
    timezone: str

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return validate_email_address(value)

    @field_validator("password")
    @classmethod
    def _password(cls, value: SecretStr) -> SecretStr:
        password = normalize_password(value.get_secret_value())
        if not 15 <= len(password) <= 128:
            raise ValueError("Password length is outside the allowed range.")
        try:
            password.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Password contains invalid Unicode.") from None
        return SecretStr(password)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        return validate_timezone(value)


class UserProfile(BaseModel):
    id: UUID
    email: str
    timezone: str
