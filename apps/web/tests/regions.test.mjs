import assert from "node:assert/strict";
import { test } from "node:test";
import { compileDetails, containsControlCharacters } from "../src/regions.ts";

test("names and detail keys reject every C0 character without expanding the rejected range", () => {
    for (let code = 0; code < 0x20; code++) {
        const value = `Fictional${String.fromCharCode(code)}name`;
        assert.equal(containsControlCharacters(value), true, `C0 ${code}`);
        assert.throws(
            () => compileDetails([{ id: "fixture", key: value, value: "value" }], new Set()),
            { message: "invalid-detail" },
        );
    }
    for (const value of [
        "",
        " ",
        "Fictional name",
        "中文",
        "\u007f",
        "\u0080",
        "🪙",
        "\ud800",
        "\udfff",
    ]) {
        assert.equal(containsControlCharacters(value), false);
    }
});

test("detail compilation preserves Unicode, intentional spaces and existing empty-field rules", () => {
    const rows = [
        { id: "empty", key: "", value: "" },
        { id: "builtin", key: "bank_name", value: "" },
        { id: "custom", key: " Fictional key ", value: " value ", custom: true },
        { id: "unicode", key: "中文🪙\u007f", value: "", custom: true },
        { id: "newline", key: "Notes", value: "line one\nline two", custom: true },
    ];
    assert.deepEqual(compileDetails(rows, new Set(["bank_name"])), {
        " Fictional key ": " value ",
        "中文🪙\u007f": "",
        Notes: "line one\nline two",
    });
});

test("detail validation keeps reserved, duplicate, invalid and count error identities", () => {
    const row = (key, value = "", custom = true) => ({ id: "fixture", key, value, custom });
    const cases = [
        [[row("bank_name")], new Set(["bank_name"]), "reserved-detail"],
        [[row("same"), row("same")], new Set(), "duplicate-detail"],
        [[row(" ")], new Set(), "invalid-detail"],
        [[row("long".repeat(17))], new Set(), "invalid-detail"],
        [[row("valid", "null\0value")], new Set(), "invalid-detail"],
        [[row("valid", "x".repeat(2001))], new Set(), "invalid-detail"],
        [
            Array.from({ length: 33 }, (_, index) => row(`field-${index}`)),
            new Set(),
            "too-many-details",
        ],
    ];
    for (const [rows, builtins, message] of cases) {
        assert.throws(() => compileDetails(rows, builtins), { message });
    }
});
