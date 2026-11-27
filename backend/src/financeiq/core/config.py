"""Validated backend settings without implicit files or import-time loading."""

from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from re import fullmatch
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    SettingsError,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

_ENV_PREFIX = "FINANCEIQ_"


class ConfigurationError(RuntimeError):
    """Configuration failure whose message contains no input values."""


class _SettingsBase(BaseSettings):
    """Shared source validation, secret handling and safe diagnostics."""

    model_config = SettingsConfigDict(
        env_prefix=_ENV_PREFIX,
        env_file=None,
        env_file_encoding="utf-8",
        env_ignore_empty=False,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    @field_validator("database_url", "migration_database_url", check_fields=False)
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        try:
            url = make_url(raw)
            valid = (
                url.drivername == "postgresql+asyncpg"
                and bool(url.username)
                and bool(url.host)
                and bool(url.database)
                and (url.port is None or 1 <= url.port <= 65535)
                and not any(character.isspace() for character in raw)
            )
        except ArgumentError, ValueError:
            valid = False
        if not valid:
            raise ValueError("Invalid PostgreSQL connection URL.") from None
        return value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        if isinstance(dotenv_settings, DotEnvSettingsSource):
            known_names = {
                f"{_ENV_PREFIX}{field}".lower(): f"{_ENV_PREFIX}{field.upper()}"
                for field in cls.model_fields
            }
            # The upstream source drops unknown empty/bare keys and unprefixed
            # field names. Validate these before it can silently discard them.
            if any(name not in known_names for name in dotenv_settings.env_vars):
                raise ConfigurationError("Unknown settings in the environment file.")
            missing_values = sorted(
                known_names[name]
                for name, value in dotenv_settings.env_vars.items()
                if value is None
            )
            if missing_values:
                raise ConfigurationError(
                    f"Missing configuration values. Check: {', '.join(missing_values)}."
                )
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    @classmethod
    def _settings_log_debug(cls, states: dict[str, dict[str, object]]) -> None:
        """Disable raw-source dumps, even when upstream debug logging is enabled.

        This hook belongs to pinned pydantic-settings 2.16.2. Its behavior is
        covered by a regression test to protect future dependency updates.
        """


class Settings(_SettingsBase):
    """Runtime settings contain no migration credentials."""

    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_url: SecretStr = Field(repr=False)
    public_origin: str | None = None
    auth_rate_limit_secret: SecretStr | None = Field(default=None, repr=False)
    auth_trusted_proxies: tuple[IPv4Address | IPv6Address, ...] = ()

    @field_validator("public_origin")
    @classmethod
    def validate_public_origin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        authority = value.removeprefix("https://")
        if authority == value or any(
            character in authority for character in "/\\@?#*,"
        ):
            raise ValueError("Expected one HTTPS origin.")
        url = AnyHttpUrl(value)
        origin = str(url).removesuffix("/")
        if url.port == 0 or (
            value != origin and not (url.port == 443 and value == f"{origin}:443")
        ):
            raise ValueError("Expected one HTTPS origin.")
        return origin

    @field_validator("auth_rate_limit_secret")
    @classmethod
    def validate_auth_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not fullmatch(
            r"[0-9a-fA-F]{64}", value.get_secret_value()
        ):
            raise ValueError("Invalid Auth rate limit secret.")
        return value

    @field_validator("auth_trusted_proxies")
    @classmethod
    def validate_auth_proxies(
        cls, value: tuple[IPv4Address | IPv6Address, ...]
    ) -> tuple[IPv4Address | IPv6Address, ...]:
        if any("%" in str(address) for address in value):
            raise ValueError("Invalid trusted proxy address.")
        return value


class MigrationSettings(_SettingsBase):
    """Migration credentials are required independently of runtime settings."""

    migration_database_url: SecretStr = Field(repr=False)


def load_settings(*, env_file: Path | None = None) -> Settings:
    """Read process variables, an optional explicit UTF-8 file, then defaults.

    Relative paths use the working directory. No files are discovered, no global
    settings are cached, and neither os.environ nor logging is configured.
    """
    return _load_settings(Settings, env_file=env_file)


def load_migration_settings(*, env_file: Path | None = None) -> MigrationSettings:
    """Load only migration configuration; never fall back to runtime credentials."""
    return _load_settings(MigrationSettings, env_file=env_file)


def _load_settings[T: _SettingsBase](
    settings_type: type[T], *, env_file: Path | None
) -> T:
    try:
        if env_file is not None and not env_file.is_file():
            raise ConfigurationError(
                "The selected environment file is missing or is not a regular file."
            )
        return settings_type(_env_file=env_file)
    except ValidationError as error:
        errors = error.errors(
            include_input=False, include_context=False, include_url=False
        )
        # Never echo arbitrary locations: unknown dotenv keys can contain secrets.
        fields = sorted(
            {
                f"{_ENV_PREFIX}{issue['loc'][0].upper()}"
                for issue in errors
                if issue["loc"]
                and isinstance(issue["loc"][0], str)
                and issue["loc"][0] in settings_type.model_fields
            }
        )
        message = "Invalid configuration."
        if any(issue["type"] == "extra_forbidden" for issue in errors):
            message += " Unknown settings in the environment file."
        if fields:
            message += f" Check: {', '.join(fields)}."
        raise ConfigurationError(message) from None
    except UnicodeError:
        raise ConfigurationError(
            "The selected environment file must use UTF-8 encoding."
        ) from None
    except OSError, SettingsError:
        raise ConfigurationError("Unable to read configuration sources.") from None
