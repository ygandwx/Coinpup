import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";
registerHooks({
    resolve(specifier, context, next) {
        return next(
            specifier.startsWith(".") &&
                !/\.[a-z]+$/u.test(specifier) &&
                context.parentURL?.includes("/src/")
                ? `${specifier}.ts`
                : specifier,
            context,
        );
    },
});
const { recurringFields, validRecurringFields, usableRecurringSource, recurringCreate } =
    await import("../src/recurring-fields.ts");
const fields = () => ({
    ...recurringFields("2026-01-31"),
    name: "Fictional 月末",
    timezone: "Asia/Shanghai",
});
const source = () => ({
    id: "fictional-source",
    version: 3,
    document_kind: "invoice",
    state: "draft",
    archived: false,
    total_amount: "99999999999999999999.99",
    asset_id: "USD",
    issue_date: "2026-01-31",
});

test("calendar form rejects invalid ranges and never guesses a missing timezone", () => {
    assert.equal(recurringFields("2026-01-31").timezone, "");
    assert.equal(validRecurringFields(fields(), false), true);
    for (const changes of [
        { name: " \t" },
        { name: "x".repeat(161) },
        { name: "Fictional\0" },
        { timezone: "" },
        { timezone: " UTC" },
        { timezone: "UTC\0" },
        { anchor: "2026-02-29" },
        { anchor: "2026-04-31" },
        { anchor: "0000-01-01" },
        { interval: "0" },
        { interval: "121" },
        { interval: "1.5" },
        { interval: "01" },
        { frequency: "hour" },
    ])
        assert.equal(
            validRecurringFields({ ...fields(), ...changes }, false),
            false,
            JSON.stringify(changes),
        );
    assert.equal(
        validRecurringFields({ ...fields(), anchor: "2028-02-29", interval: "120" }, false),
        true,
    );
});

test("creation captures the selected version without converting or copying its prices", () => {
    const original = source(),
        input = fields(),
        before = structuredClone(original);
    assert.deepEqual(recurringCreate("fictional-rule", input, original), {
        id: "fictional-rule",
        name: input.name,
        timezone_name: input.timezone,
        anchor_date: "2026-01-31",
        frequency: "month",
        interval_count: 1,
        source_document_id: original.id,
        source_version: 3,
    });
    assert.deepEqual(original, before);
    for (const value of [
        null,
        { ...source(), archived: true },
        { ...source(), document_kind: "bill" },
        { ...source(), state: "posted" },
    ]) {
        assert.equal(usableRecurringSource(value), false);
        assert.throws(() => recurringCreate("fictional-rule", input, value));
    }
});

test("existing rules display their saved calendar and rename validation does not rewrite it", () => {
    const saved = {
        name: "Fictional saved",
        timezone_name: "Europe/Tallinn",
        anchor_date: "2024-02-29",
        frequency: "year",
        interval_count: 2,
    };
    const value = recurringFields("2026-10-10", saved);
    assert.deepEqual(value, {
        name: saved.name,
        timezone: saved.timezone_name,
        anchor: saved.anchor_date,
        frequency: "year",
        interval: "2",
    });
    assert.equal(validRecurringFields({ ...value, name: "Fictional renamed" }, true), true);
    assert.equal(saved.name, "Fictional saved");
});
