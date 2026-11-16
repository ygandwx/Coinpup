"""Database connectivity only; schema changes are performed with Alembic."""

from typing import Protocol

from sqlalchemy import create_engine, event, text

from coinpup_api.config import Settings


class DatabaseProbe(Protocol):
    def check(self) -> None: ...

    def close(self) -> None: ...


def _use_iso_dates(connection, _record) -> None:
    # Psycopg's text loader needs ISO timestamps, including on hosts that set PGDATESTYLE.
    if not connection.info.parameter_status("DateStyle").startswith("ISO,"):
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute("SET SESSION DateStyle TO 'ISO, YMD'")


class Database:
    def __init__(self, settings: Settings) -> None:
        self.engine = create_engine(
            settings.database_url.get_secret_value(),
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={
                "connect_timeout": settings.database_connect_timeout,
                "options": "-c statement_timeout=3000",
            },
        )
        event.listen(self.engine, "connect", _use_iso_dates, insert=True)

    def check(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def close(self) -> None:
        self.engine.dispose()
