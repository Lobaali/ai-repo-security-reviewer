"""
config.py - loading and validating all settings in one place.

Aim of this file
----------------
Every tunable value (model name, timeouts, retries and the processing limits)
is defined and validated here, then passed to the other components as a 
`Settings` object. No other module reads files or environment
variables for configuration, which means:
  - tests can build a `Settings` with tiny limits directly in code;
  - changing a limit is a config edit, not a code change;
  - an invalid value (e.g. max_model_calls: -5 or a misspelled key) stops the
    program immediately with a clear message ("fail fast").

Secrets are kept separate from settings:
  - config.yaml       -> limits, timeouts, model name (safe to share)
  - OPENAI_API_KEY    -> environment variable or git-ignored .env file
The key is stored as a Pydantic SecretStr, so printing or logging the settings
shows '**********' instead of the real value.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator


class ConfigError(Exception):
    """Raised when configuration is missing or invalid. The CLI turns this
    into a clear error message and a non-zero exit code."""


# ---------------------------------------------------------------------------
# Settings sections (mirror the sections of config.example.yaml)
# ---------------------------------------------------------------------------
# extra="forbid" rejects unknown keys, so a typo such as "max_file:" instead
# of "max_files:" is an error rather than silently using the default.

class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="gpt-5-mini", min_length=1)
    request_timeout_seconds: float = Field(default=60, gt=0, le=600)
    max_retries: int = Field(default=2, ge=0, le=5)


class RepositorySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clone_timeout_seconds: float = Field(default=120, gt=0, le=1800)


class Limits(BaseModel):
    """The explicit processing limits"""

    model_config = ConfigDict(extra="forbid")

    max_file_bytes: int = Field(default=100_000, gt=0)
    max_files: int = Field(default=40, gt=0)
    max_total_chars: int = Field(default=400_000, gt=0)
    chunk_chars: int = Field(default=20_000, gt=0)
    max_model_calls: int = Field(default=60, gt=0)
    investigator_max_tool_calls: int = Field(default=3, ge=0)
    reviewer_max_tool_calls: int = Field(default=5, ge=0)
    tool_output_max_chars: int = Field(default=4_000, gt=0)

    @model_validator(mode="after")
    def _check_consistency(self) -> Limits:
        """Catch combinations that make no sense, even if each value is valid alone."""
        if self.chunk_chars > self.max_total_chars:
            raise ValueError("chunk_chars cannot be larger than max_total_chars")
        return self


class Settings(BaseModel):
    """Everything the application needs to run, already validated."""

    model_config = ConfigDict(extra="forbid")

    model: ModelSettings = Field(default_factory=ModelSettings)
    repository: RepositorySettings = Field(default_factory=RepositorySettings)
    limits: Limits = Field(default_factory=Limits)
    # Not read from YAML; filled from the environment by load_settings().
    openai_api_key: SecretStr | None = None

    def require_api_key(self) -> str:
        """Return the real key, or raise ConfigError if it is missing.

        Called only when a real model client is created, so tests (which use
        a fake client) never need a key.
        """
        if self.openai_api_key is None or not self.openai_api_key.get_secret_value().strip():
            raise ConfigError(
                "OPENAI_API_KEY is not set. Put it in a .env file (see .env.example) "
                "or export it in your shell."
            )
        return self.openai_api_key.get_secret_value()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_settings(config_path: Path | None = None) -> Settings:
    """Build Settings from an optional YAML file plus the environment.

    Args:
        config_path: path to a YAML file. If None, built-in defaults are used
            (the same values as config.example.yaml).

    Raises:
        ConfigError: the file is missing, is not valid YAML, or contains
            invalid values. The message says exactly what is wrong.
    """
    data: dict = {}
    if config_path is not None:
        if not config_path.is_file():
            raise ConfigError(f"config file not found: {config_path}")
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"config file {config_path} is not valid YAML: {exc}") from exc
        if loaded is not None and not isinstance(loaded, dict):
            raise ConfigError(f"config file {config_path} must contain a mapping at the top level")
        data = loaded or {}
        if "openai_api_key" in data:
            # Secrets must never live in a config file that might be shared.
            raise ConfigError("do not put openai_api_key in the config file; use OPENAI_API_KEY")

    # Load a local .env file if present. Existing environment variables win,
    # so an exported OPENAI_API_KEY is never overwritten by the file.
    load_dotenv(Path.cwd() / ".env", override=False)
    api_key = os.environ.get("OPENAI_API_KEY")

    try:
        return Settings(**data, openai_api_key=api_key or None)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration:\n{exc}") from exc