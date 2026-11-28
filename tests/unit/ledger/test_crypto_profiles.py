import base64
import binascii
import hashlib

import pytest
from coinpup_api.ledger.crypto_profiles import (
    names_known_platform,
    normalized_platform_name,
    supports_deposit_network,
    valid_deposit_address,
    valid_deposit_memo,
)

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58(value):
    encoded = ""
    number = int.from_bytes(value, "big")
    while number:
        number, remainder = divmod(number, 58)
        encoded = _ALPHABET[remainder] + encoded
    return "1" * (len(value) - len(value.lstrip(b"\x00"))) + encoded


def _fictional_base58check(prefix):
    payload = bytes([prefix]) + b"\x8d" * 20
    return _base58(payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4])


def test_platform_names_are_stable_for_case_spacing_and_unicode_width():
    assert normalized_platform_name("  Ｆｉｃｔｉｏｎａｌ   WALLET ") == normalized_platform_name(
        "fictional wallet"
    )
    assert normalized_platform_name("fictional wallet") != normalized_platform_name("other wallet")
    assert names_known_platform("Ｂｉｎａｎｃｅ")
    assert names_known_platform("Binance Exchange")
    assert names_known_platform("币安")
    assert names_known_platform("OKX Web3 Wallet")
    assert names_known_platform("欧易 Web3 钱包")
    assert names_known_platform("BiTgEt Web3 Wallet")
    assert not names_known_platform("Fictional wallet")


@pytest.mark.parametrize(
    "network", ["ethereum", "bsc", "polygon", "arbitrum", "optimism", "base", "avalanche"]
)
def test_evm_addresses_have_twenty_hex_bytes(network):
    assert valid_deposit_address(network, "0x" + "aB" * 20)
    assert not valid_deposit_address(network, "0x" + "aB" * 19)
    assert not valid_deposit_address(network, "0x" + "Ze" * 20)
    assert not valid_deposit_address(network, " " + "0x" + "aB" * 20)


@pytest.mark.parametrize(
    ("network", "prefix"),
    [
        ("bitcoin", 0),
        ("bitcoin", 5),
        ("litecoin", 48),
        ("litecoin", 50),
        ("dogecoin", 30),
        ("dogecoin", 22),
        ("tron", 65),
    ],
)
def test_base58_addresses_respect_prefix_and_checksum(network, prefix):
    address = _fictional_base58check(prefix)
    assert valid_deposit_address(network, address)
    assert not valid_deposit_address(network, address[:-1] + ("1" if address[-1] != "1" else "2"))
    assert not valid_deposit_address(network, _fictional_base58check(111))


def test_known_bounded_address_formats_and_custom_network():
    assert valid_deposit_address("tron", "41" + "ab" * 20)
    assert not valid_deposit_address("tron", "42" + "ab" * 20)
    assert valid_deposit_address("solana", _base58(b"\x00" + b"\x8d" * 31))
    assert not valid_deposit_address("solana", _base58(b"\x8d" * 33))
    assert not valid_deposit_address("solana", "1" * 31)
    assert valid_deposit_address("bitcoin", "bc1" + "q" * 40)
    assert not valid_deposit_address("bitcoin", "tb1" + "q" * 40)
    assert not valid_deposit_address("bitcoin", "BC1q" + "q" * 39)
    assert valid_deposit_address("litecoin", "ltc1" + "q" * 40)
    assert valid_deposit_address("monero", "4" + "x" * 105)
    assert not valid_deposit_address("monero", "8" + "x" * 105)
    assert valid_deposit_address("cardano", "addr1q" + "q" * 60)
    assert not valid_deposit_address("cardano", "stake1" + "q" * 30)
    assert valid_deposit_address("xrpl", "r" + "A" * 27)
    assert not valid_deposit_address("xrpl", "r" + "0" * 27)
    assert valid_deposit_address("fictional-net", "Synthetic:Addr-01")
    assert not valid_deposit_address("fictional-net", "twelve wallet words")


def test_ton_and_ss58_preserve_public_network_formats():
    ton_record = b"\x11\x00" + b"\xab" * 32
    ton = base64.urlsafe_b64encode(
        ton_record + binascii.crc_hqx(ton_record, 0).to_bytes(2, "big")
    ).decode()
    assert valid_deposit_address("ton", ton)
    assert valid_deposit_address("ton", "0:" + "aB" * 32)
    assert not valid_deposit_address("ton", "1:" + "aB" * 32)
    assert not valid_deposit_address("ton", ("B" if ton[0] == "A" else "A") + ton[1:])
    for prefix in [0, 2]:
        record = bytes([prefix]) + b"\x8d" * 32
        code = _base58(record + hashlib.blake2b(b"SS58PRE" + record).digest()[:2])
        assert valid_deposit_address("polkadot", code) is (prefix == 0)


def test_tags_and_issuer_networks_are_specific_to_the_asset():
    classic = "r" + "A" * 27
    assert valid_deposit_memo("xrpl", classic, "000001")
    assert valid_deposit_memo("xrpl", classic, "4294967295")
    assert not valid_deposit_memo("xrpl", classic, "4294967296")
    assert not valid_deposit_memo("xrpl", "X" + "A" * 46, "1")
    assert valid_deposit_memo("ton", None, "fictional memo")
    assert supports_deposit_network("USDT", "tron")
    assert supports_deposit_network("BTC", "bitcoin")
    assert not supports_deposit_network("BTC", "ethereum")
    assert not supports_deposit_network("USDC", "tron")
