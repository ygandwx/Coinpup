import assert from "node:assert/strict";
import { test } from "node:test";
import {
    cryptoAccountDraft,
    cryptoAccountUpdates,
    cryptoAssetChoices,
} from "../src/crypto-account-draft.ts";

test("verified presets reuse exact stored EVM address identities and respect disabled records", () => {
    const reference = "0x" + "Aa".repeat(20);
    const preset = {
        asset_id: `token:ethereum:${reference}`,
        code: "ABC",
        kind: "token",
        network: "ethereum",
        scale: 6,
        token_reference: reference,
    };
    const saved = {
        ...preset,
        asset_id: preset.asset_id.toLowerCase(),
        token_reference: reference.toLowerCase(),
        enabled: false,
        verified: true,
    };
    const custodial = {
        asset_id: "custodial:ABC",
        code: "ABC",
        kind: "custodial",
        network: null,
        scale: 6,
        token_reference: null,
    };
    assert.deepEqual(cryptoAssetChoices([saved], [preset, custodial]), [
        saved,
        { ...custodial, enabled: true, verified: true, needsRegistration: true },
    ]);
});

test("a coin edit leaves untargeted and dormant profiles unchanged", () => {
    const original = cryptoAccountDraft({
        crypto_platform: { institution_id: "binance" },
        crypto_assets: [
            {
                asset_id: "custodial:BTC",
                details: { network: "bitcoin", label: "Fictional reserve" },
            },
            {
                asset_id: "custodial:USDT",
                details: { network: "tron", label: "Fictional settlement" },
            },
        ],
    });
    const changed = structuredClone(original);
    changed.coins["custodial:BTC"].label = "New reserve label";
    delete changed.coins["custodial:USDT"];
    assert.deepEqual(cryptoAccountUpdates(original, changed, ["custodial:BTC"]), {
        crypto_assets: [
            {
                asset_id: "custodial:BTC",
                details: {
                    network: "bitcoin",
                    address: null,
                    memo: null,
                    label: "New reserve label",
                },
            },
        ],
    });
    assert.deepEqual(
        cryptoAccountUpdates(original, original, ["custodial:BTC", "custodial:USDT"]),
        {},
    );
});
