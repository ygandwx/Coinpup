import assert from "node:assert/strict";
import { test } from "node:test";
import { id, setupReturn } from "./return-revision-fixture.mjs";
const { loadReturnSourcePage } = await import("../src/return-revision-sources.ts");
const { validReturnRevisionInput } = await import("../src/allocation-return-revision-intent.ts");
test("scoped cursor reads include retained original allocations on the first page and never fetch duplicate groups", async () => {
    const { review } = setupReturn(),
        { current, original, capacities } = review,
        calls = [];
    const page = async (offset, groups) =>
        loadReturnSourcePage(original, current, offset, new AbortController().signal, {
            list: async (l, p, o) => {
                assert.equal(l, current.ledger_id);
                assert.equal(p, current.payment_id);
                assert.equal(o, offset);
                return groups;
            },
            capacity: async (l, g) => {
                assert.equal(l, current.ledger_id);
                calls.push(g);
                return capacities.find((c) => c.id === g);
            },
        });
    assert.deepEqual(
        (await page(0, [capacities[1]])).capacities.map((c) => c.id),
        [capacities[1].id, capacities[0].id],
    );
    calls.length = 0;
    assert.equal((await page(0, [capacities[0]])).more, false);
    assert.deepEqual(calls, [capacities[0].id]);
    calls.length = 0;
    assert.deepEqual((await page(25, [])).capacities, []);
    assert.deepEqual(calls, []);
});
test("foreign/malformed capacities and superseded pages are rejected before a draft can use their quantities", async () => {
    const { review } = setupReturn(),
        { current, original, capacities } = review;
    for (const patch of [
        { id: id(81) },
        { ledger_id: id(81) },
        { payment_id: id(81) },
        { asset_id: "EUR" },
        { lines: [] },
        { lines: [capacities[0].lines[0], capacities[0].lines[0]] },
    ]) {
        await assert.rejects(
            loadReturnSourcePage(original, current, 0, new AbortController().signal, {
                list: async () => [capacities[0]],
                capacity: async () => ({ ...capacities[0], ...patch }),
            }),
            (e) => e.code === "invalid_response",
        );
    }
    const stop = new AbortController();
    stop.abort();
    await assert.rejects(
        loadReturnSourcePage(original, current, 0, stop.signal, {
            list: async () => [capacities[0]],
            capacity: async () => assert.fail("aborted page did work"),
        }),
        (e) => e.name === "AbortError",
    );
});
test("revision reason length follows its normalized text; replacement reason keeps its raw field limit", () => {
    const { correct } = setupReturn();
    correct.body.reason = " " + "x".repeat(1000) + " ";
    assert.ok(validReturnRevisionInput(correct));
    correct.body.replacement.reason = " " + "x".repeat(2000);
    assert.equal(validReturnRevisionInput(correct), false);
});
