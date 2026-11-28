"""Authoritative platform keys and offline shape checks for public deposit addresses."""

import base64
import binascii
import hashlib
import re
import unicodedata
from collections import defaultdict

from coinpup_api.ledger.assets import (
    _EVM_NETWORKS,
    CUSTODIAL_PRESETS,
    NATIVE_PRESETS,
    TOKEN_PRESETS,
)

# Catalog labels and aliases from apps/web/src/institutions.ts. Catalog IDs are also labels.
_PLATFORMS = {
    "binance": ("custodial", ("币安", "幣安", "Binance Exchange")),
    "okx": ("custodial", ("欧易", "歐易", "欧易交易所", "OKEx", "OKX Exchange")),
    "coinbase": ("custodial", ("Coinbase Advanced", "Coinbase Exchange")),
    "kraken": ("custodial", ("Kraken Exchange",)),
    "bybit": ("custodial", ("Bybit Exchange",)),
    "bitget": ("custodial", ("Bitget Exchange", "Bitget 交易所")),
    "gate": ("custodial", ("Gate.io", "Gateio", "芝麻开门", "Gate Exchange")),
    "htx": ("custodial", ("火币 HTX", "火币", "火幣", "Huobi", "Huobi Global", "HTX Exchange")),
    "cryptomus": ("custodial", ("Cryptomus Wallet",)),
    "metamask": ("onchain", ("Meta Mask", "小狐狸钱包", "小狐狸")),
    "trust-wallet": ("onchain", ("TrustWallet", "Trust 钱包")),
    "imtoken": ("onchain", ("im Token", "imToken Wallet")),
    "tronlink": ("onchain", ("Tron Link", "TronLink Pro", "波宝钱包")),
    "phantom": ("onchain", ("Phantom Wallet", "幻影钱包")),
    "okx-web3-wallet": (
        "onchain",
        ("欧易 Web3 钱包", "OKX Wallet", "欧易钱包", "OKX Web3", "OKX Web3钱包"),
    ),
    "bitget-wallet": ("onchain", ("Bitget 钱包", "Bitget Web3 Wallet", "BitKeep", "Bitget钱包")),
    "cake-wallet": ("onchain", ("CakeWallet", "Cake 钱包")),
    "ledger": ("onchain", ("Ledger Live", "Ledger Nano", "Ledger Wallet")),
    "trezor": ("onchain", ("Trezor Suite", "Trezor Wallet")),
    "onekey": ("onchain", ("One Key", "OneKey Wallet")),
}
PLATFORM_MODES = {platform: mode for platform, (mode, _) in _PLATFORMS.items()}
CRYPTO_PRESETS = {
    entry.definition.asset_id: entry.definition
    for entry in (*NATIVE_PRESETS, *TOKEN_PRESETS, *CUSTODIAL_PRESETS)
}
_DEPOSIT_NETWORKS = defaultdict(set)
for asset in CRYPTO_PRESETS.values():
    if asset.network is not None:
        _DEPOSIT_NETWORKS[asset.code].add(asset.network)

_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_CHARS = re.compile(f"[{_BASE58}]+")
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def normalized_platform_name(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def _catalog_spelling(name: str) -> str:
    return "".join(char for char in normalized_platform_name(name) if char.isalnum())


_CATALOG_NAMES = {
    _catalog_spelling(name)
    for platform, (_, aliases) in _PLATFORMS.items()
    for name in (platform, *aliases)
}


def names_known_platform(name: str) -> bool:
    return _catalog_spelling(name) in _CATALOG_NAMES


def supports_deposit_network(code: str, network: str) -> bool:
    return network in _DEPOSIT_NETWORKS.get(code, ())


def _decode_base58(value: str) -> bytes:
    if not _BASE58_CHARS.fullmatch(value):
        return b""
    number = 0
    for char in value:
        number = number * 58 + _BASE58.index(char)
    pad = len(value) - len(value.lstrip("1"))
    return b"\x00" * pad + number.to_bytes((number.bit_length() + 7) // 8, "big")


def _base58check(value: str, prefix: set[int]) -> bool:
    data = _decode_base58(value)
    if len(data) != 25 or data[0] not in prefix:
        return False
    checksum = hashlib.sha256(hashlib.sha256(data[:-4]).digest()).digest()[:4]
    return data[-4:] == checksum


def _bech32_shape(value: str, prefix: str) -> bool:
    return (value == value.lower() or value == value.upper()) and bool(
        re.fullmatch(f"{prefix}1[{_BECH32}]{{11,120}}", value.lower())
    )


def _ton_address(value: str) -> bool:
    if re.fullmatch(r"(?:0|-1):[0-9A-Fa-f]{64}", value):
        return True
    if not re.fullmatch(r"(?:[A-Za-z0-9+/]{48}|[A-Za-z0-9_-]{48})", value):
        return False
    data = base64.b64decode(value, altchars=b"-_", validate=True)
    return (
        data[0] in {0x11, 0x51}
        and data[1] in {0, 255}
        and binascii.crc_hqx(data[:-2], 0) == int.from_bytes(data[-2:], "big")
    )


def valid_deposit_address(network: str, address: str) -> bool:
    if not 1 <= len(address) <= 128 or not address.isascii() or not address.isprintable():
        return False
    if network in _EVM_NETWORKS or network == "evm":
        return re.fullmatch(r"0x[0-9A-Fa-f]{40}", address) is not None
    if network == "tron":
        # https://developers.tron.network/docs/encoding
        return _base58check(address, {0x41}) or bool(re.fullmatch(r"41[0-9A-Fa-f]{40}", address))
    if network == "solana":
        # https://solana.com/docs/core/accounts/account-structure
        return len(_decode_base58(address)) == 32
    if network == "ton":
        # https://docs.ton.org/foundations/addresses/overview
        return _ton_address(address)
    if network == "bitcoin":
        # https://github.com/bitcoin/bips/blob/master/bip-0350.mediawiki
        return _base58check(address, {0, 5}) or (
            len(address) <= 90 and _bech32_shape(address, "bc")
        )
    if network == "litecoin":
        # https://github.com/litecoin-project/litecoin/blob/master/src/chainparams.cpp
        return _base58check(address, {5, 48, 50}) or (
            len(address) <= 90 and _bech32_shape(address, "ltc")
        )
    if network == "dogecoin":
        # https://github.com/dogecoin/dogecoin/blob/master/src/chainparams.cpp
        return _base58check(address, {22, 30})
    if network == "xrpl":
        # https://xrpl.org/docs/concepts/accounts/addresses
        return re.fullmatch(f"(?:r[{_BASE58}]{{24,34}}|X[{_BASE58}]{{46}})", address) is not None
    if network == "cardano":
        # https://cips.cardano.org/cip/CIP-19
        return _bech32_shape(address, "addr") or bool(
            re.fullmatch(f"(?:Ae2|DdzFF)[{_BASE58}]{{45,120}}", address)
        )
    if network == "polkadot":
        # https://github.com/paritytech/ss58-registry/blob/main/ss58-registry.json
        data = _decode_base58(address)
        return (
            len(data) == 35
            and data[0] == 0
            and data[-2:] == hashlib.blake2b(b"SS58PRE" + data[:-2]).digest()[:2]
        )
    if network == "monero":
        # https://www.getmonero.org/library/MoneroAddressesCheatsheet20201206.pdf
        return re.fullmatch(f"(?:[48][{_BASE58}]{{94}}|4[{_BASE58}]{{105}})", address) is not None
    # An explicitly registered custom network has no protocol-specific format here.
    return re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", address) is not None


def valid_deposit_memo(network: str, address: str | None, memo: str) -> bool:
    if network != "xrpl":
        return True
    # https://xrpl.org/docs/concepts/transactions/source-and-destination-tags
    return (
        (address is None or not address.startswith("X"))
        and re.fullmatch(r"[0-9]{1,10}", memo) is not None
        and int(memo) <= 4_294_967_295
    )
