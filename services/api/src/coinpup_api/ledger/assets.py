"""Immutable quantity definitions, without network calls or token precision guesses."""

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from coinpup_api.ledger.errors import MoneyError

_FIAT_CODES = frozenset({"USD", "GBP", "EUR", "HKD", "CNY"})
_FIAT_CODE = re.compile(r"[A-Z]{3}")
_NATIVE = MappingProxyType({"BTC": ("bitcoin", 8), "ETH": ("ethereum", 18), "XMR": ("monero", 12)})
_NETWORK = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_TOKEN_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


@dataclass(frozen=True, slots=True)
class AssetDefinition:
    code: str
    kind: Literal["fiat", "native", "token"]
    scale: int
    network: str | None = None
    token_reference: str | None = None
    # Availability is not part of unit identity: historical quantities remain decodable.
    enabled: bool = field(default=True, compare=False)

    def __post_init__(self):
        if type(self.scale) is not int or not 0 <= self.scale <= 18:
            raise MoneyError("asset_scale", "Asset scale must be an integer between 0 and 18.")
        if type(self.enabled) is not bool:
            raise MoneyError("asset_definition", "Asset enabled must be a boolean.")
        if not isinstance(self.code, str) or not isinstance(self.kind, str):
            raise MoneyError("asset_definition", "Asset code and kind must be strings.")
        if self.kind == "fiat":
            valid = (
                _FIAT_CODE.fullmatch(self.code) is not None
                and self.code not in _NATIVE
                and (self.code not in _FIAT_CODES or self.scale == 2)
                and self.network is None
                and self.token_reference is None
            )
        elif self.kind == "native":
            valid = (
                self.code in _NATIVE
                and (self.network, self.scale) == _NATIVE[self.code]
                and self.token_reference is None
            )
        elif self.kind == "token":
            valid = (
                self.code in {"USDT", "USDC"}
                and isinstance(self.network, str)
                and _NETWORK.fullmatch(self.network) is not None
                and isinstance(self.token_reference, str)
                and _TOKEN_REFERENCE.fullmatch(self.token_reference) is not None
            )
        else:
            valid = False
        if not valid:
            raise MoneyError("asset_definition", "Asset identity and precision are inconsistent.")

    @property
    def asset_id(self) -> str:
        if self.kind == "token":
            # Token references retain their case; some networks use case-sensitive identifiers.
            return f"token:{self.network}:{self.token_reference}"
        return self.code


ASSETS = MappingProxyType(
    {
        **{code: AssetDefinition(code, "fiat", 2) for code in sorted(_FIAT_CODES)},
        **{
            code: AssetDefinition(code, "native", scale, network=network)
            for code, (network, scale) in _NATIVE.items()
        },
    }
)


def get_asset(asset_id: str) -> AssetDefinition:
    if not isinstance(asset_id, str) or asset_id not in ASSETS:
        raise MoneyError("unknown_asset", "Asset is not in the built-in quantity catalog.")
    return ASSETS[asset_id]


def stablecoin_asset(
    *, code: str, network: str, token_reference: str, scale: int, enabled: bool = True
) -> AssetDefinition:
    """Define an explicitly configured token; does not register or verify an on-chain contract."""
    return AssetDefinition(code, "token", scale, network, token_reference, enabled)


def fiat_asset(*, code: str, scale: int, enabled: bool = True) -> AssetDefinition:
    """Configure an original currency explicitly; this is not an authoritative currency lookup."""
    return AssetDefinition(code, "fiat", scale, enabled=enabled)
