import assert from "node:assert/strict";
import { test } from "node:test";
import { AmountError, amountFromMinorUnits, formatAmount, parseAmount, sumAmounts } from "../src/money.ts";

test("20 integer digits and 18 fractional digits survive formatting and round trip", () => {
  const text = "99999999999999999999.999999999999999999";
  assert.equal(parseAmount(text, 18), 99999999999999999999999999999999999999n);
  assert.equal(amountFromMinorUnits(parseAmount(text, 18), 18), text);
  assert.equal(formatAmount(text, "en"), "99,999,999,999,999,999,999.999999999999999999");
  assert.equal(formatAmount(`-${text}`, "zh"), "-99,999,999,999,999,999,999.999999999999999999");
});

test("display preserves fractional padding and uses the locale decimal separator", () => {
  assert.equal(formatAmount("0.89999000", "en"), "0.89999000");
  assert.equal(formatAmount("1234.000001", "de-DE"), "1.234,000001");
  assert.equal(formatAmount("0.000000000000000001", "zh"), "0.000000000000000001");
});

test("preview addition never rounds, including a final net total at the storage bound", () => {
  assert.equal(sumAmounts(["0.1", "0.2", "-0.3"], 2), "0.00");
  assert.equal(sumAmounts(["1", "-0.1", "-0.00001"], 8), "0.89999000");
  assert.equal(sumAmounts(["99999999999999999999", "1", "-1"], 0), "99999999999999999999");
  assert.equal(amountFromMinorUnits(parseAmount("-0.00", 2), 2), "0.00");
});

test("strict wire format rejects lossy or ambiguous inputs before arithmetic", () => {
  for (const text of ["1e3", "1,000", "+1", "01", ".1", "1.", " 1", "1 ", "NaN", "Infinity", "１２", "1\n"]) {
    assert.throws(() => parseAmount(text, 18), (error) => error instanceof AmountError && error.code === "amount_format");
  }
  assert.throws(() => parseAmount("1.000", 2), { code: "amount_precision" });
  assert.throws(() => parseAmount("0.0000000000000000001", 18), { code: "amount_precision" });
  assert.throws(() => parseAmount("100000000000000000000", 0), { code: "amount_range" });
  assert.throws(() => sumAmounts(["99999999999999999999", "1"], 0), { code: "amount_range" });
  assert.throws(() => parseAmount("1", 19), { code: "asset_scale" });
});
