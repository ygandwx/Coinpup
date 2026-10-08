import assert from "node:assert/strict";
import { test } from "node:test";
import { initialEntry, savedEntry, preparedEntry } from "../src/ocr-entry.ts";

const assets = [{ asset_id: "USD", code: "USD", enabled: true }];
function review(source = "ocr") {
    return {
        fields: [
            ["total", "10.00"],
            ["currency", "USD"],
            ["document_date", "2031-07-18"],
        ].map(([role, value]) => ({
            path: `header.${role}`,
            source,
            candidate_value: value,
            suggested_value: source === "text" ? value : null,
            requires_confirmation: source !== "text",
        })),
    };
}
test("OCR never prefills until its individual field is reviewed and use is explicit", () => {
    const state = review(),
        paths = state.fields.map((f) => f.path);
    for (const [confirmed, use] of [
        [[], true],
        [paths, false],
    ]) {
        const input = initialEntry("expense", state, assets, confirmed, use);
        assert.equal(input.body.amount, "");
        assert.equal(input.body.asset_id, "");
        assert.equal(input.body.transaction_date, "");
        assert.equal(input.body.account_id, "");
    }
    const accepted = initialEntry("expense", state, assets, paths, true);
    assert.equal(accepted.body.amount, "10.00");
    assert.equal(accepted.body.asset_id, "USD");
    assert.equal(accepted.body.recognition_date, "");
    state.fields[0].suggested_value = "999";
    assert.equal(initialEntry("expense", state, assets, [], false).body.amount, "");
});
test("certain text prefills exact strings, but ambiguous values or asset identities stay blank", () => {
    const state = review("text");
    assert.equal(initialEntry("income", state, assets, [], false).body.amount, "10.00");
    state.fields.push({ ...state.fields[0], path: "other.amount", suggested_value: "12.00" });
    const input = initialEntry(
        "income",
        state,
        [...assets, { asset_id: "fictional:USD", code: "USD", enabled: true }],
        [],
        false,
    );
    assert.equal(input.body.amount, "");
    assert.equal(input.body.asset_id, "");
});
test("untyped persisted reviews cannot crash the form, and valid command snapshots stay unchanged", () => {
    const entry = preparedEntry(initialEntry("expense", review("text"), assets, [], false));
    assert.equal(savedEntry(entry), entry);
    for (const value of [
        null,
        [],
        { kind: ["expense"], command: entry.command },
        { ...entry, command: { ...entry.command, splits: {} } },
        { kind: "opening", command: { ...entry.command, splits: null } },
        { ...entry, command: { ...entry.command, amount: 10 } },
        { ...entry, command: { ...entry.command, fees: [null] } },
    ])
        assert.equal(savedEntry(value), null);
});
