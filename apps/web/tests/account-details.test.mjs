import assert from "node:assert/strict";
import { test } from "node:test";
import {
    accountFields,
    defaultAssetIds,
    isAccountAssetVisible,
    maskCardNumber,
    validCardDay,
    validCardExpiry,
    validCardLastFour,
    validCardNumber,
    validCreditLimit,
    validWalletAddress,
} from "../src/account-details.ts";

test("account details reflect payment type and bank region", () => {
    for (const [region, field] of [
        ["CN", "cnaps_code"],
        ["HK", "hk_bank_code"],
        ["US", "routing_number"],
        ["EE", "iban"],
        ["GB", "iban"],
        ["EU", "iban"],
    ]) {
        const fields = accountFields("bank", region);
        assert.ok(fields.includes("institution"));
        assert.ok(fields.includes(field));
        assert.equal(fields.includes("wallet_address"), false);
    }
    assert.deepEqual(accountFields("cash", "US"), ["cash_location", "note"]);
    assert.deepEqual(accountFields("wechat", "CN"), ["account_number", "real_name", "note"]);
    for (const platform of ["paypal", "wise", "stripe"])
        assert.deepEqual(accountFields(platform, "CN"), ["email", "payment_account_id"]);
    assert.ok(accountFields("credit_card", "").includes("card_last_four"));
    assert.equal(accountFields("credit_card", "").includes("account_number"), false);
    assert.ok(accountFields("debit_card", "").includes("linked_account_number"));
    assert.equal(accountFields("debit_card", "").includes("credit_limit"), false);
});

test("asset filters preserve deliberate or saved non-default holdings", () => {
    const cny = { asset_id: "CNY", kind: "fiat", enabled: true };
    const eth = { asset_id: "ETH", kind: "native", enabled: true };
    const token = { asset_id: "USDC:solana:test", kind: "token", enabled: true };
    assert.deepEqual(defaultAssetIds("alipay", [cny, eth]), ["CNY"]);
    assert.deepEqual(defaultAssetIds("wechat", [{ ...cny, enabled: false }, eth]), []);
    assert.deepEqual(defaultAssetIds("bank", [cny]), []);
    assert.equal(isAccountAssetVisible(eth, "bank", [], false), false);
    assert.equal(isAccountAssetVisible(eth, "bank", ["ETH"], false), true);
    assert.equal(isAccountAssetVisible(eth, "bank", [], true), true);
    assert.equal(isAccountAssetVisible(cny, "crypto", [], false), false);
    assert.equal(isAccountAssetVisible(token, "crypto", [], false), true);
});

test("wallet validation checks public address shapes by network", () => {
    const evm = "0x" + "a0".repeat(20);
    const tron = "T" + "x".repeat(33);
    const solana = "1".repeat(32);
    assert.equal(validWalletAddress("ethereum", evm), true);
    assert.equal(validWalletAddress("ethereum", evm.slice(0, -1)), false);
    assert.equal(validWalletAddress("ethereum", solana), false);
    assert.equal(validWalletAddress("tron", tron), true);
    assert.equal(validWalletAddress("tron", "41" + "ab".repeat(20)), true);
    assert.equal(validWalletAddress("tron", evm), false);
    assert.equal(validWalletAddress("solana", solana), true);
    assert.equal(validWalletAddress("solana", "0" + solana), false);
    assert.equal(validWalletAddress("bitcoin", "bc1" + "q".repeat(39)), true);
    assert.equal(validWalletAddress("bitcoin", "BC1q" + "q".repeat(39)), false);
    assert.equal(validWalletAddress("monero", "4" + "x".repeat(94)), true);
    assert.equal(validWalletAddress("monero", "8" + "x".repeat(94)), true);
    assert.equal(validWalletAddress("monero", "4" + "x".repeat(105)), true);
    assert.equal(validWalletAddress("monero", "8" + "x".repeat(105)), false);
    assert.equal(validWalletAddress("monero", "4O" + "x".repeat(93)), false);
    assert.equal(validWalletAddress("custom", "two words"), false);
    assert.equal(validWalletAddress("ethereum", ""), true);
});

test("card numbers, masks, dates and exact credit limits obey their display and entry rules", () => {
    assert.equal(validCardNumber("0000000000000018"), true);
    assert.equal(validCardNumber("0000000000000019"), false);
    assert.equal(validCardNumber("00000000000"), false);
    assert.equal(validCardNumber("00000000000000000000"), false);
    assert.equal(validCardNumber("000000000000001８"), false);
    assert.equal(validCardExpiry("01/37"), true);
    assert.equal(validCardExpiry("12/37"), true);
    assert.equal(validCardExpiry("13/37"), false);
    assert.equal(validCardExpiry("00/37"), false);
    assert.equal(validCardExpiry("01/2037"), false);
    assert.equal(validCreditLimit("12345678901234567890.123456789012345678"), true);
    assert.equal(validCreditLimit("0.1234567890123456789"), false);
    assert.equal(validCreditLimit("1e18"), false);
    assert.equal(validCreditLimit("-0.01"), false);
    assert.equal(
        maskCardNumber({ kind: "debit_card", details: { card_last_four: "0018" } }),
        "•••• 0018",
    );
    assert.equal(
        maskCardNumber({ kind: "debit_card", details: { card_last_four: "0000000000000018" } }),
        undefined,
    );
    assert.equal(maskCardNumber({ kind: "bank", details: { card_last_four: "0018" } }), undefined);
    assert.equal(validCardLastFour("0009"), true);
    assert.equal(validCardLastFour("1234-5678-9012-3456"), false);
    assert.equal(validCardLastFour("１２３４"), false);
    assert.equal(validCardDay("1"), true);
    assert.equal(validCardDay("31"), true);
    assert.equal(validCardDay("32"), false);
    assert.equal(validCardDay("0"), false);
});
