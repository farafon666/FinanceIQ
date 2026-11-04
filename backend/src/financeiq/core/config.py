"""Validated backend settings without implicit files or import-time loading."""

from pathlib import Path
from typing import Literal

from pydantic import ValidationError
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    SettingsError,
)

_ENV_PREFIX = "FINANCEIQ_"


class ConfigurationError(RuntimeError):
    """Configuration failure whose message contains no input values."""


class Settings(BaseSettings):
    """An immutable settings snapshot; use load_settings for safe diagnostics."""

    model_config = SettingsConfigDict(
        env_prefix=_ENV_PREFIX,
        env_file=None,
        env_file_encoding="utf-8",
        env_ignore_empty=False,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

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


def load_settings(*, env_file: Path | None = None) -> Settings:
    """Read process variables, an optional explicit UTF-8 file, then defaults.

    Relative paths use the working directory. No files are discovered, no global
    settings are cached, and neither os.environ nor logging is configured.
    """
    try:
        if env_file is not None and not env_file.is_file():
            raise ConfigurationError(
                "The selected environment file is missing or is not a regular file."
            )
        return Settings(_env_file=env_file)
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
                and issue["loc"][0] in Settings.model_fields
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
