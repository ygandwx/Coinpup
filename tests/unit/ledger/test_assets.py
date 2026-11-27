"""Asset identity, immutable precision and explicit stablecoin configuration."""

from dataclasses import FrozenInstanceError, replace

import pytest
from coinpup_api.ledger import (
    ASSETS,
    Amount,
    AssetDefinition,
    MoneyError,
    fiat_asset,
    get_asset,
    stablecoin_asset,
)
from coinpup_api.ledger.assets import NATIVE_PRESETS, TOKEN_PRESETS, verified_token


def test_builtin_quantity_definitions_are_fixed_and_read_only():
    assert {code: asset.scale for code, asset in ASSETS.items()} == {
        "USD": 2,
        "GBP": 2,
        "EUR": 2,
        "HKD": 2,
        "CNY": 2,
        "BTC": 8,
        "ETH": 18,
        "XMR": 12,
    }
    with pytest.raises(TypeError):
        ASSETS["USD"] = get_asset("EUR")
    with pytest.raises(FrozenInstanceError):
        get_asset("BTC").scale = 2
    with pytest.raises(MoneyError) as error:
        replace(get_asset("BTC"), scale=2)
    assert error.value.code == "asset_definition"


@pytest.mark.parametrize("code", ["USDT", "USDC", "usd", "UNKNOWN", None, []])
def test_unknown_or_unconfigured_assets_are_not_guessed(code):
    with pytest.raises(MoneyError) as error:
        get_asset(code)
    assert error.value.code == "unknown_asset"


def test_stablecoin_identity_preserves_case_and_requires_all_configuration():
    first = stablecoin_asset(code="USDT", network="test-chain", token_reference="TokenA", scale=6)
    second = stablecoin_asset(code="USDT", network="test-chain", token_reference="tokena", scale=6)
    other_network = replace(first, network="other-test-chain")
    assert first.asset_id == "token:test-chain:TokenA"
    assert len({first.asset_id, second.asset_id, other_network.asset_id}) == 3
    with pytest.raises(TypeError):
        stablecoin_asset(code="USDT", network="test-chain", token_reference="TokenA")
    assert "USDT" not in ASSETS


@pytest.mark.parametrize(
    "changes",
    [
        {"scale": True},
        {"scale": -1},
        {"scale": 19},
        {"scale": 6.0},
        {"network": ""},
        {"network": " chain"},
        {"network": "chain:1"},
        {"token_reference": ""},
        {"token_reference": "Token:Ambiguous"},
        {"token_reference": "代币"},
        {"code": "bTC"},
        {"code": "T"},
        {"code": "TOKENCODE00"},
        {"enabled": 1},
    ],
)
def test_factory_refuses_ambiguous_or_invalid_asset_configuration(changes):
    values = {"code": "USDC", "network": "test-chain", "token_reference": "TokenA", "scale": 6}
    with pytest.raises(MoneyError):
        stablecoin_asset(**(values | changes))


def test_direct_definition_cannot_override_native_or_fiat_units():
    with pytest.raises(MoneyError):
        AssetDefinition("USD", "fiat", 3)
    with pytest.raises(MoneyError):
        AssetDefinition("ETH", "native", 18, network="bitcoin")
    with pytest.raises(MoneyError):
        AssetDefinition("USDT", "token", 6)
    with pytest.raises(MoneyError):
        fiat_asset(code="BTC", scale=2)


def test_explicit_original_currencies_are_not_limited_to_display_currencies():
    jpy = fiat_asset(code="JPY", scale=0)
    assert jpy.asset_id == "JPY"
    assert Amount.parse("123", jpy).to_string() == "123"
    assert "JPY" not in ASSETS
    assert len(ASSETS) == 8
    with pytest.raises(MoneyError):
        Amount.parse("123.1", jpy)
    with pytest.raises(MoneyError):
        fiat_asset(code="USD", scale=3)
    with pytest.raises(MoneyError):
        fiat_asset(code="jpy", scale=0)
    with pytest.raises(MoneyError):
        fiat_asset(code="ＪＰＹ", scale=0)


@pytest.mark.parametrize("changes", [{"scale": 18}, {"code": "USDC"}])
def test_same_token_id_with_conflicting_definition_cannot_be_combined(changes):
    asset = stablecoin_asset(code="USDT", network="test-chain", token_reference="TokenA", scale=6)
    conflicting = replace(asset, **changes)
    assert asset.asset_id == conflicting.asset_id
    assert asset != conflicting
    left, right = Amount.parse("1", asset), Amount.parse("1", conflicting)
    assert left != right
    for operation in (lambda: left + right, lambda: left - right, lambda: left < right):
        with pytest.raises(MoneyError) as error:
            operation()
        assert error.value.code == "asset_mismatch"


def test_expanded_native_presets_keep_original_fiat_identities():
    assert {item.definition.code: item.definition.scale for item in NATIVE_PRESETS} == {
        "BTC": 8,
        "ETH": 18,
        "SOL": 9,
        "BNB": 18,
        "TRX": 6,
        "XRP": 6,
        "DOGE": 8,
        "LTC": 8,
        "ADA": 6,
        "TON": 9,
        "AVAX": 18,
        "DOT": 10,
        "POL": 18,
        "XMR": 12,
    }
    sol = get_asset("native:solana:SOL")
    fiat = fiat_asset(code="SOL", scale=2)
    assert sol != fiat
    assert (sol.asset_id, fiat.asset_id) == ("native:solana:SOL", "SOL")
    assert Amount.parse("0.000000001", sol).minor_units == 1
    with pytest.raises(MoneyError, match="fractional digits"):
        Amount.parse("0.0000000001", sol)
    assert all(item.sources for item in NATIVE_PRESETS)


def test_preset_evidence_is_scoped_to_exact_network_contract_and_case():
    assert len(TOKEN_PRESETS) == 20
    by_id = {item.definition.asset_id: item.definition for item in TOKEN_PRESETS}
    assert len(by_id) == len(TOKEN_PRESETS)
    evm = by_id["token:ethereum:0xdac17f958d2ee523a2206206994597c13d831ec7"]
    assert verified_token("ethereum", evm.token_reference.upper()) is evm
    assert verified_token("optimism", evm.token_reference) is None
    solana = next(item for item in by_id.values() if item.network == "solana")
    assert verified_token(solana.network, solana.token_reference) is solana
    assert verified_token(solana.network, solana.token_reference.lower()) is None
    assert verified_token("tron", "FictionalContractReference") is None
