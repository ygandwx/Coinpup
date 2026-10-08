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
const { reviewedInput } = await import("../src/ocr-batch.ts");
const entry = {
    kind: "expense",
    command: {
        account_id: "fictional-bank",
        asset_id: "USD",
        amount: "10.00",
        transaction_date: "2031-01-01",
        recognition_date: "2031-01-01",
        splits: [],
    },
};
const review = () => ({
    draft_id: "fictional-draft",
    version: 3,
    status: "draft",
    fields: [{ path: "header.total", requires_confirmation: true }],
    review: { entry, confirmed: ["header.total"] },
});
test("batch keeps the saved action, exact amount, version and selected destination", () => {
    const state = review();
    const input = reviewedInput("fictional-ledger", "fictional-file", state);
    assert.equal(input.body.entry, entry);
    assert.equal(input.body.entry.command.amount, "10.00");
    assert.equal(input.body.expected_version, 3);
    assert.equal(input.body.duplicate_ack, false);
    state.review.confirmed.length = 0;
    assert.deepEqual(input.body.confirmed, ["header.total"]);
    const next = review();
    next.review.entry = {
        kind: "destination",
        version: 1,
        ledger_id: "11111111-1111-4111-8111-111111111111",
        file_id: "22222222-2222-4222-8222-222222222222",
        action: entry,
    };
    const moved = reviewedInput("source", "file", next);
    assert.equal(moved.body.target_ledger_id, next.review.entry.ledger_id);
    assert.equal(moved.body.entry, entry);
});
test("batch refuses ignored, confirmed, missing action or unreviewed fields without skipping them", () => {
    for (const change of [
        (r) => {
            r.status = "ignored";
        },
        (r) => {
            r.status = "confirmed";
        },
        (r) => {
            r.review.entry = {};
        },
        (r) => {
            r.review.confirmed = [];
        },
    ]) {
        const state = review();
        change(state);
        assert.throws(() => reviewedInput("source", "file", state), {
            code: "ocr_batch_not_ready",
        });
    }
});
