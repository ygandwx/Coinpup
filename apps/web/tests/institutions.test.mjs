import assert from "node:assert/strict";
import { test } from "node:test";
import {
    BANK_INSTITUTIONS,
    CRYPTO_PLATFORMS,
    INSTITUTIONS,
    accountInstitution,
    getInstitution,
} from "../src/institutions.ts";

test("institution ids and verified BIC values are unique and each record has an official source", () => {
    assert.equal(new Set(INSTITUTIONS.map((item) => item.id)).size, INSTITUTIONS.length);
    const withSwift = INSTITUTIONS.filter((item) => item.swiftPrefix);
    assert.equal(new Set(withSwift.map((item) => item.swiftPrefix)).size, withSwift.length);
    for (const institution of INSTITUTIONS) {
        assert.equal(getInstitution(institution.id), institution);
        assert.match(institution.source, /^https:\/\//);
        if (institution.swiftPrefix) {
            assert.match(institution.swiftPrefix, /^[A-Z]{6}[A-Z0-9]{2}$/);
            assert.match(institution.swiftSource, /^https:\/\//);
        }
    }
    for (const id of ["abc-cn", "za-hk", "wise-eu", "ifast-gb", "hsbc-cn", "hsbc-hk"]) {
        assert.ok(
            BANK_INSTITUTIONS.some((institution) => institution.id === id),
            id,
        );
    }
    assert.equal(CRYPTO_PLATFORMS.length, 20);
    assert.equal(getInstitution("binance")?.custody, "custodial");
    assert.equal(getInstitution("okx-web3-wallet")?.custody, "self-custody");
});

test("honor the explicit selection and preserve ambiguous and conflicting legacy records", () => {
    assert.equal(accountInstitution({ institution: "lhv-ee", bank_name: "HSBC" })?.id, "lhv-ee");
    assert.equal(accountInstitution({ swift_bic: " hsbchkhhhkh " })?.id, "hsbc-hk");
    assert.equal(accountInstitution({ bank_name: "中国工商银行" })?.id, "icbc-cn");
    assert.equal(accountInstitution({ bank_name: "HSBC" }), undefined);
    assert.equal(accountInstitution({ institution: "unknown", bank_name: "LHV Bank" }), undefined);
    assert.equal(accountInstitution({ bank_name: "建设银行", swift_bic: "ABOC" }), undefined);
    assert.equal(accountInstitution({ bank_name: "HSBC", swift_bic: "HSBCHKHH" })?.id, "hsbc-hk");
});

test("recognize legacy bank names, issuer names, and SWIFT institution prefixes without mutating details", () => {
    const details = {};
    for (const name of [
        "农业银行",
        "农行",
        "中國農業銀行",
        "中国农业银行工资账户",
        "Agricultural Bank of China",
    ]) {
        assert.equal(accountInstitution(details, name, "bank")?.id, "abc-cn", name);
    }
    assert.deepEqual(details, {});
    assert.equal(
        accountInstitution({ issuer_name: "农行" }, "Primary card", "debit_card")?.id,
        "abc-cn",
    );
    assert.equal(accountInstitution({ swift_bic: "ABOC" }, undefined, "bank")?.id, "abc-cn");
    assert.equal(accountInstitution({ swift_bic: "ABOCCNBJ110" }, undefined, "bank")?.id, "abc-cn");
    assert.equal(accountInstitution({}, "工行与农行", "bank"), undefined);
    assert.equal(accountInstitution({}, "Momo's cash", "cash"), undefined);
    assert.equal(accountInstitution({}, "农业银行", "crypto"), undefined);
    assert.equal(accountInstitution({}, "HSBC", "bank"), undefined);
});

test("associate legacy crypto platform and fixed payment types with their own institution", () => {
    assert.equal(accountInstitution({}, "币安 USDT", "crypto")?.id, "binance");
    assert.equal(accountInstitution({}, "OKX Wallet", "crypto")?.id, "okx-web3-wallet");
    assert.equal(accountInstitution({}, "My WeChat payout", "wechat")?.id, "wechat");
    assert.equal(accountInstitution({}, "Wise transfer", "wise")?.id, "wise-eu");
});

test("hidden institution metadata cannot leak across account type changes", () => {
    const bank = { institution: "abc-cn", bank_name: "农业银行" };
    assert.equal(accountInstitution(bank, "农业银行", "cash"), undefined);
    assert.equal(accountInstitution(bank, "农业银行", "paypal")?.id, "paypal");
    assert.equal(accountInstitution(bank, "农业银行", "crypto"), undefined);
    assert.equal(
        accountInstitution({ institution: "binance" }, "Former exchange", "bank"),
        undefined,
    );
    assert.equal(accountInstitution(bank, "New debit card", "debit_card")?.id, "abc-cn");
});
