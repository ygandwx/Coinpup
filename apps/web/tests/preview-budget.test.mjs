import assert from "node:assert/strict";
import { test } from "node:test";
import { previewSize } from "../src/preview-budget.ts";

test("portrait, landscape and extreme page shapes stay within canvas bounds", () => {
    for (const [width, height] of [
        [300, 200],
        [600, 800],
        [1, 100000],
        [100000, 1],
        [100000, 100000],
    ]) {
        const size = previewSize(width, height);
        assert.ok(size.width >= 1 && size.width <= 1600);
        assert.ok(size.height >= 1 && size.height <= 2400);
        assert.ok(size.width * size.height <= 4_000_000);
    }
});
test("invalid PDF page dimensions are rejected before allocating a canvas", () => {
    for (const value of [0, -1, NaN, Infinity, 100001]) {
        assert.throws(() => previewSize(value, 100), /preview_dimensions/u);
        assert.throws(() => previewSize(100, value), /preview_dimensions/u);
    }
});
