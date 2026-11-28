"""Private field operations for the running API; the provisioned key is pinned per process."""

from functools import cache

from coinpup_private_v1 import PrivateFieldError, load_key
from sqlalchemy import Connection, Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from coinpup_api.models import PrivateFieldKeyCheck

_runtime_key = cache(load_key)


def encrypt(value, table, field, ledger_id, account_id, asset_id=None) -> str:
    return _runtime_key().encrypt(value, table, field, ledger_id, account_id, asset_id)


def decrypt(value, table, field, ledger_id, account_id, asset_id=None) -> str:
    return _runtime_key().decrypt(value, table, field, ledger_id, account_id, asset_id)


def custom_platform_key(normalized_name, ledger_id) -> str:
    return _runtime_key().custom_platform_key(normalized_name, ledger_id)


def verify_storage_key(connection: Connection | Session) -> None:
    """Check this database in the same transaction that will write a private profile."""
    envelope = connection.scalar(
        select(PrivateFieldKeyCheck.verifier).where(PrivateFieldKeyCheck.singleton == 1)
    )
    if envelope is None or decrypt(envelope, "private_field_key_check", "verifier", "", "1") != (
        "coinpup-key-check"
    ):
        raise PrivateFieldError


def verify_database_key(engine: Engine) -> None:
    try:
        with engine.connect() as connection:
            verify_storage_key(connection)
    except SQLAlchemyError:
        raise PrivateFieldError from None
