"""Database connectivity only; schema changes are performed with Alembic."""

from typing import Protocol

from sqlalchemy import create_engine, text

from coinpup_api.config import Settings


class DatabaseProbe(Protocol):
    def check(self) -> None: ...

    def close(self) -> None: ...


class Database:
    def __init__(self, settings: Settings) -> None:
        self.engine = create_engine(
            settings.database_url.get_secret_value(),
            pool_pre_ping=True,
            connect_args={
                "connect_timeout": settings.database_connect_timeout,
                "options": "-c statement_timeout=3000",
            },
        )

    def check(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def close(self) -> None:
        self.engine.dispose()
