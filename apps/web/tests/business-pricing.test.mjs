import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
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
const { priceBusinessLine, priceBusinessDocument } = await import("../src/business-pricing.ts");
const vectors = JSON.parse(
    readFileSync(new URL("../../../tests/fixtures/business-pricing.json", import.meta.url), "utf8"),
);
for (const vector of vectors.cases) {
    test(vector.name, () => {
        const before = JSON.stringify(vector.lines);
        if (vector.error)
            assert.throws(() => priceBusinessDocument(vector.lines, vector.scale), {
                code: vector.error,
            });
        else assert.deepEqual(priceBusinessDocument(vector.lines, vector.scale), vector.expected);
        assert.equal(JSON.stringify(vector.lines), before);
    });
}

test("all quantity/rate syntax is bounded ASCII with no coercion or trimming", () => {
    const source = { quantity: "1", unit_price: "1" };
    for (const value of [
        null,
        true,
        1,
        "",
        "01",
        "1e0",
        "1\n",
        " 1",
        "１",
        "1." + "0".repeat(19),
        "9".repeat(21),
    ]) {
        for (const [field, code] of [
            ["quantity", "pricing_quantity"],
            ["tax_rate_percent", "pricing_tax_rate"],
        ]) {
            assert.throws(() => priceBusinessLine({ ...source, [field]: value }, 2), { code });
        }
    }
    assert.throws(() => priceBusinessLine({ ...source, quantity: "-1" }, 2), {
        code: "pricing_quantity",
    });
    assert.throws(() => priceBusinessLine({ ...source, tax_rate_percent: "-1" }, 2), {
        code: "pricing_tax_rate",
    });
    for (const field of ["unit_price", "discount_amount"]) {
        for (const value of [null, true, 1]) {
            assert.throws(() => priceBusinessLine({ ...source, [field]: value }, 2), {
                code: "amount_type",
            });
        }
    }
    assert.throws(() => priceBusinessLine({ ...source, discount_amount: "-1" }, 2), {
        code: "pricing_discount",
    });
});

test("empty and 200-line drafts validate precision and reject oversize or malformed inputs", () => {
    const line = Object.freeze({ quantity: "1", unit_price: "0.01" });
    const source = Object.freeze(Array(200).fill(line));
    assert.equal(priceBusinessDocument(source, 2).total_amount, "2.00");
    assert.throws(() => priceBusinessDocument([...source, line], 2), { code: "pricing_lines" });
    assert.throws(() => priceBusinessDocument(null, 2), { code: "pricing_lines" });
    assert.throws(() => priceBusinessDocument([null], 2), { code: "pricing_line" });
    for (const scale of [-1, 19, 2.5, "2"]) {
        assert.throws(() => priceBusinessDocument([], scale), { code: "asset_scale" });
    }
});
