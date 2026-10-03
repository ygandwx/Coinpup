"""Explicit environment configuration; secrets are never included in health output."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
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
    allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://127.0.0.1:8000",
            "http://localhost:8000",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    session_ttl_seconds: int = Field(default=43200, ge=60, le=604800)
    login_max_failures: int = Field(default=5, ge=1, le=20)
    login_window_seconds: int = Field(default=900, ge=60, le=86400)
    login_lock_seconds: int = Field(default=900, ge=60, le=86400)

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, values: list[str]) -> list[str]:
        if not values:
            raise ValueError("At least one explicit origin is required")
        result = []
        for value in values:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or "*" in value
            ):
                raise ValueError("Origins must be explicit HTTP(S) origins without paths")
            host = parsed.hostname.lower()
            if ":" in host:
                host = f"[{host}]"
            port = parsed.port
            if port and (parsed.scheme, port) not in {("http", 80), ("https", 443)}:
                host += f":{port}"
            result.append(f"{parsed.scheme}://{host}")
        return list(dict.fromkeys(result))

    @model_validator(mode="after")
    def production_requires_https_origins(self):
        if self.environment == "production" and (
            "allowed_origins" not in self.model_fields_set
            or any(not origin.startswith("https://") for origin in self.allowed_origins)
        ):
            raise ValueError("Production requires explicitly configured HTTPS allowed_origins")
        return self

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
