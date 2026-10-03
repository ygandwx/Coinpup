"""Explicit environment configuration; secrets are never included in health output."""

from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="COINPUP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    environment: Literal["development", "test", "production"] = "development"
    database_url: SecretStr = SecretStr("postgresql+psycopg://coinpup@localhost:5432/coinpup")
    database_connect_timeout: int = Field(default=3, ge=1, le=10)

    @field_validator("database_url")
    @classmethod
    def require_postgresql(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
        except ArgumentError:
            raise ValueError("A valid PostgreSQL connection URL is required") from None
        if url.drivername != "postgresql+psycopg" or not url.database:
            raise ValueError("Use postgresql+psycopg with an explicit database")
        return value
