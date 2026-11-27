"""Immutable quantity definitions, without network calls or token precision guesses."""

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from coinpup_api.ledger.asset_evidence import NATIVE_EVIDENCE, TOKEN_EVIDENCE
from coinpup_api.ledger.errors import MoneyError

_FIAT_CODES = frozenset({"USD", "GBP", "EUR", "HKD", "CNY"})
_FIAT_CODE = re.compile(r"[A-Z]{3}")
_LEGACY_NATIVE_CODES = frozenset({"BTC", "ETH", "XMR"})
_NATIVE = MappingProxyType({code: (network, scale) for code, network, scale, _ in NATIVE_EVIDENCE})
_NETWORK = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_TOKEN_CODE = re.compile(r"[A-Z0-9]{2,10}")
_TOKEN_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_EVM_NETWORKS = frozenset(
    {"ethereum", "bsc", "polygon", "arbitrum", "optimism", "base", "avalanche"}
)
_EVM_ADDRESS = re.compile(r"0[xX][0-9A-Fa-f]{40}")
_CUSTODIAL_SCALES = {code: scale for code, (_, scale) in _NATIVE.items()}
for _code, _, _, _scale, _ in TOKEN_EVIDENCE:
    _CUSTODIAL_SCALES[_code] = max(_CUSTODIAL_SCALES.get(_code, 0), _scale)


@dataclass(frozen=True, slots=True)
class AssetDefinition:
    code: str
    kind: Literal["fiat", "native", "token", "custodial"]
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
                and self.code not in _LEGACY_NATIVE_CODES
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
        elif self.kind == "custodial":
            valid = (
                self.scale == _CUSTODIAL_SCALES.get(self.code)
                and self.network is None
                and self.token_reference is None
            )
        elif self.kind == "token":
            valid = (
                _TOKEN_CODE.fullmatch(self.code) is not None
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
        if self.kind == "custodial":
            return f"custodial:{self.code}"
        if self.kind == "token":
            # Token references retain their case; some networks use case-sensitive identifiers.
            return f"token:{self.network}:{self.token_reference}"
        if self.kind == "native" and self.code not in _LEGACY_NATIVE_CODES:
            # The original fiat API allowed arbitrary three-letter codes, including later coins.
            return f"native:{self.network}:{self.code}"
        return self.code


def evm_token_reference(network: str | None, token_reference: str | None) -> str | None:
    """Return the comparison key for a hexadecimal address on a supported EVM network."""
    if network in _EVM_NETWORKS and token_reference and _EVM_ADDRESS.fullmatch(token_reference):
        return token_reference.lower()
    return None


def _token_key(network, token_reference):
    return network, evm_token_reference(network, token_reference) or token_reference


@dataclass(frozen=True, slots=True)
class AssetEvidence:
    definition: AssetDefinition
    sources: tuple[str, ...]


NATIVE_PRESETS = tuple(
    AssetEvidence(AssetDefinition(code, "native", scale, network), sources)
    for code, network, scale, sources in NATIVE_EVIDENCE
)
TOKEN_PRESETS = tuple(
    AssetEvidence(AssetDefinition(code, "token", scale, network, reference), sources)
    for code, network, reference, scale, sources in TOKEN_EVIDENCE
)
_VERIFIED_TOKENS = MappingProxyType(
    {
        _token_key(preset.definition.network, preset.definition.token_reference): preset.definition
        for preset in TOKEN_PRESETS
    }
)


def verified_token(network: str | None, token_reference: str | None) -> AssetDefinition | None:
    """Find the issuer-verified identity, independent of a user-provided symbol or scale."""
    return _VERIFIED_TOKENS.get(_token_key(network, token_reference))


def custodial_family(asset: AssetDefinition) -> str | None:
    """Resolve a custody family only from a verified complete asset definition."""
    if asset.kind in {"custodial", "native"}:
        return asset.code
    if asset.kind == "token":
        official = verified_token(asset.network, asset.token_reference)
        if official is not None and (asset.code, asset.scale) == (official.code, official.scale):
            return official.code
    return None


def _custodial_presets():
    sources = {}
    for preset in (*NATIVE_PRESETS, *TOKEN_PRESETS):
        sources.setdefault(preset.definition.code, {}).update(dict.fromkeys(preset.sources))
    return tuple(
        AssetEvidence(AssetDefinition(code, "custodial", scale), tuple(sources[code]))
        for code, scale in _CUSTODIAL_SCALES.items()
    )


CUSTODIAL_PRESETS = _custodial_presets()


# Keep the initial database seed distinct from the expanded presets, which are opt-in.
ASSETS = MappingProxyType(
    {
        **{code: AssetDefinition(code, "fiat", 2) for code in sorted(_FIAT_CODES)},
        **{
            preset.definition.asset_id: preset.definition
            for preset in NATIVE_PRESETS
            if preset.definition.code in _LEGACY_NATIVE_CODES
        },
    }
)
_BUILTINS = ASSETS | {
    preset.definition.asset_id: preset.definition
    for preset in (*NATIVE_PRESETS, *CUSTODIAL_PRESETS)
}


def get_asset(asset_id: str) -> AssetDefinition:
    if not isinstance(asset_id, str) or asset_id not in _BUILTINS:
        raise MoneyError("unknown_asset", "Asset is not in the built-in quantity catalog.")
    return _BUILTINS[asset_id]


def stablecoin_asset(
    *, code: str, network: str, token_reference: str, scale: int, enabled: bool = True
) -> AssetDefinition:
    """Define an explicitly configured token; does not register or verify an on-chain contract."""
    return AssetDefinition(code, "token", scale, network, token_reference, enabled)


def fiat_asset(*, code: str, scale: int, enabled: bool = True) -> AssetDefinition:
    """Configure an original currency explicitly; this is not an authoritative currency lookup."""
    return AssetDefinition(code, "fiat", scale, enabled=enabled)
