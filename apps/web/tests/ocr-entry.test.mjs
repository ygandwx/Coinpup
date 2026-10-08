import assert from "node:assert/strict";
import { test } from "node:test";
import {
    initialEntry,
    savedEntry,
    savedAction,
    preparedEntry,
    savedDestination,
    destinationEntry,
    replaceAction,
} from "../src/ocr-entry.ts";

test("destination wrapper preserves exact actions and can explicitly clear old ledger references", () => {
    const target = {
        ledger: "11111111-1111-4111-8111-111111111111",
        file: "22222222-2222-4222-8222-222222222222",
    };
    const action = { kind: "link", operation_id: "fictional", expected_version: 2 };
    const wrapper = destinationEntry(target, action);
    assert.deepEqual(savedDestination(wrapper), target);
    assert.equal(savedAction(wrapper), action);
    const cleared = destinationEntry(target, null);
    assert.equal(savedAction(cleared), null);
    assert.equal(savedAction(replaceAction(cleared, action)), action);
    assert.equal(replaceAction(action, action), action);
    assert.equal(savedAction({ ...wrapper, version: 2 }), null);
    assert.equal(savedAction({ ...wrapper, ledger_id: null }), null);
});

test("saved link preserves the selected version and rejects malformed review data", () => {
    const action = { kind: "link", operation_id: "fictional-operation", expected_version: 3 };
    assert.equal(savedAction(action), action);
    assert.equal(savedEntry(action), null);
    for (const version of [0, -1, 1.5, "3", null, undefined, Infinity])
        assert.equal(savedAction({ ...action, expected_version: version }), null);
    assert.equal(savedAction({ ...action, operation_id: {} }), null);
});

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
