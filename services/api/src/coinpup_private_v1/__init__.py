"""Frozen v1 account-field storage protocol shared by the API and revision 0118.

Keep this codec and its framing readable by historical migrations. Introduce
future envelopes in a new version. No database or application dependencies.
"""

import base64
import hmac
import json
import os
import re
from pathlib import Path
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import dotenv_values


class PrivateFieldError(Exception):
    """Safe for application/server logs even when input contains a private value."""

    def __init__(self):
        super().__init__("Private account storage is unavailable")


def _frame(parts: list[str]) -> bytes:
    return json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _decode64(value: str) -> bytes:
    decoded = base64.b64decode(value, validate=True)
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError
    return decoded


class PrivateKey:
    def __init__(self, key_id: str, raw: bytes):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", key_id) or len(raw) != 32:
            raise PrivateFieldError
        self.id = key_id
        self._aead = AESGCM(raw)
        self._index_key = hmac.digest(raw, b"coinpup.custom-platform.index-key.v1", "sha256")

    def _context(self, table, field, ledger_id, account_id, asset_id) -> bytes:
        return _frame(
            [
                "coinpup.private",
                "v1",
                self.id,
                table,
                field,
                str(ledger_id),
                str(account_id),
                "" if asset_id is None else str(asset_id),
            ]
        )

    def encrypt(
        self,
        value: str,
        table: str,
        field: str,
        ledger_id: UUID | str,
        account_id: UUID | str,
        asset_id: str | None = None,
    ) -> str:
        aad = self._context(table, field, ledger_id, account_id, asset_id)
        nonce = os.urandom(12)
        sealed = self._aead.encrypt(nonce, value.encode("utf-8"), aad)
        return f"v1.{self.id}.{base64.b64encode(nonce + sealed).decode('ascii')}"

    def decrypt(
        self,
        value: str,
        table: str,
        field: str,
        ledger_id: UUID | str,
        account_id: UUID | str,
        asset_id: str | None = None,
    ) -> str:
        aad = self._context(table, field, ledger_id, account_id, asset_id)
        try:
            version, key_id, data = value.split(".")
            if version != "v1" or key_id != self.id:
                raise ValueError
            sealed = _decode64(data)
            if len(sealed) < 12 + 16:
                raise ValueError
            return self._aead.decrypt(sealed[:12], sealed[12:], aad).decode("utf-8")
        except (AttributeError, ValueError, InvalidTag):
            raise PrivateFieldError from None

    def custom_platform_key(self, normalized_name: str, ledger_id: UUID | str) -> str:
        message = _frame(["coinpup.custom-platform.v1", str(ledger_id), normalized_name])
        return "custom:" + hmac.digest(self._index_key, message, "sha256").hex()


def load_key() -> PrivateKey:
    """Read a provisioned key. Missing/invalid material never initializes a new key."""
    try:
        path = os.environ.get("COINPUP_PRIVATE_KEY_FILE")
        if path is None:
            path = dotenv_values(".env").get("COINPUP_PRIVATE_KEY_FILE")
        if not path:
            raise ValueError
        with Path(path).open("rb") as source:
            encoded = source.read(1025)
        if len(encoded) > 1024:
            raise ValueError
        document = json.loads(encoded)
        if not isinstance(document, dict) or document.keys() != {"id", "key"}:
            raise ValueError
        return PrivateKey(document["id"], _decode64(document["key"]))
    except (KeyError, OSError, TypeError, ValueError):
        raise PrivateFieldError from None
