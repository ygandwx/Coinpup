"""Authenticated storage for the private parts of account profiles."""

import json
import re

from coinpup_api import private_fields
from coinpup_api.ledger.models import Account, AccountAsset


def _context(record: Account | AccountAsset, field: str) -> tuple:
    if isinstance(record, AccountAsset):
        return ("account_assets", field, record.ledger_id, record.account_id, record.asset_id)
    return ("accounts", field, record.ledger_id, record.id)


def read_private(record: Account | AccountAsset, field: str) -> str | None:
    ciphertext = getattr(record, field + "_ciphertext")
    if ciphertext is None:
        return None
    return private_fields.decrypt(ciphertext, *_context(record, field))


def write_private(record: Account | AccountAsset, field: str, value: str | None) -> None:
    ciphertext = None
    if value is not None:
        ciphertext = private_fields.encrypt(value, *_context(record, field))
    setattr(record, field + "_ciphertext", ciphertext)


def read_details(account: Account) -> dict[str, str]:
    encoded = read_private(account, "details")
    private = json.loads(encoded) if encoded is not None else {}
    return {**private, **account.details}


def write_details(account: Account, details: dict[str, str]) -> None:
    private = details.copy()
    public = {}
    if re.fullmatch(r"[0-9]{4}", private.get("card_last_four", "")):
        public["card_last_four"] = private.pop("card_last_four")
    account.details = public
    encoded = json.dumps(private, ensure_ascii=False, separators=(",", ":")) if private else None
    write_private(account, "details", encoded)
