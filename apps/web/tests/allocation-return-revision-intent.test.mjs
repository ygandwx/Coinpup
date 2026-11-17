import assert from "node:assert/strict";
import { test } from "node:test";
import { id, setupReturn } from "./return-revision-fixture.mjs";
const { validReturnRevisionInput, validReturnRevisionReview } =
    await import("../src/allocation-return-revision-intent.ts");
test("retained sources reclaim precisely their own units on both sides, preserving the exact command text", () => {
    const { ledger, review, correct, cancel } = setupReturn();
    const raw = JSON.stringify(correct);
    const valid = (cmd, r = review) => validReturnRevisionReview(ledger, cmd, r);
    assert.ok(valid(correct));
    assert.equal(JSON.stringify(correct), raw);
    assert.ok(valid(cancel));
    for (const edit of [
        (b) => {
            b.replacement.lines[0].payment_amount = "0.10";
        },
        (b) => {
            b.replacement.lines[0].document_amount = "0.00000010";
        },
        (b) => {
            b.replacement.lines[0].payment_amount = "0.090";
        },
        (b) => {
            b.replacement.lines[0].document_amount = "0.000000091";
        },
        (b) => {
            b.replacement.lines[0].payment_amount = 0.09;
        },
        (b) => {
            b.replacement.lines[0].expected_version = 2;
        },
        (b) => {
            b.replacement.lines[0].payment_amount = "-0.01";
        },
        (b) => {
            b.expected_version = 2;
        },
        (b) => {
            b.replacement.return_date = "2025-12-31";
        },
        (b) => {
            b.replacement.lines.push({ ...b.replacement.lines[0] });
        },
        (b) => {
            b.replacement.lines = [];
        },
        (b) => {
            b.replacement.extra = true;
        },
    ]) {
        const c = structuredClone(correct);
        edit(c.body);
        assert.equal(valid(c), false);
    }
    for (const mutate of [
        (r) => {
            r.original.payment_amount = "0.01";
        },
        (r) => {
            r.original.receipt.id = id(99);
        },
        (r) => {
            r.capacities[0].payment_id = id(99);
        },
        (r) => {
            r.original.receipt.postings[0].journal_id = id(99);
        },
        (r) => {
            r.current.members[0].source_operation_id = id(99);
        },
        (r) => {
            r.capacities[0].lines[0].remaining_document_amount = "0.00000005";
        },
        (r) => {
            r.capacities[0].lines[0].source_status = "cancelled";
        },
        (r) => {
            r.capacities[0].lines[0].returned_payment_amount = "0.03";
            r.capacities[0].lines[0].remaining_payment_amount = "0.09";
        },
        (r) => {
            r.capacities.push(r.capacities[0]);
        },
        (r) => {
            r.assets.push(r.assets[0]);
        },
        (r) => {
            r.capacities[0].ledger_id = id(99);
        },
        (r) => {
            r.assets[1].enabled = false;
        },
    ]) {
        const r = structuredClone(review);
        mutate(r);
        assert.equal(valid(correct, r), false);
    }
    const retired = structuredClone(review);
    retired.assets.forEach((a) => (a.enabled = false));
    retired.capacities = [];
    assert.ok(valid(cancel, retired));
    assert.equal(validReturnRevisionReview(id(77), correct, review), false);
});
test("200 replacements can swap across reviewed allocations; no source can reclaim another member's used capacity", () => {
    const { ledger, review, correct, sources, line } = setupReturn(200);
    assert.ok(validReturnRevisionReview(ledger, correct, review));
    correct.body.replacement.lines = sources.slice(200).map(line).reverse();
    assert.ok(validReturnRevisionReview(ledger, correct, review));
    correct.body.replacement.lines[0].payment_amount = "0.13";
    assert.equal(validReturnRevisionReview(ledger, correct, review), false);
    correct.body.replacement.lines.push(line(sources[0]));
    assert.equal(validReturnRevisionInput(correct), false);
});
test("terminal groups, mismatched fixed originals and unsafe revisions cannot start an intent", () => {
    const { ledger, review, cancel } = setupReturn();
    for (const update of [
        { expected_version: 2147483647 },
        { expected_version: "1" },
        { expected_version: 1.5 },
        { reason: "\0" },
        { reason: "  " },
        { reason: "x".repeat(1001) },
        { replacement: null },
    ])
        assert.equal(
            validReturnRevisionInput({ ...cancel, body: { ...cancel.body, ...update } }),
            false,
        );
    const c = review.current;
    c.status = "cancelled";
    c.version = 2;
    c.members = [];
    c.fact = null;
    c.revision_id = id(88);
    c.revision_reason = "Fictional cancel";
    cancel.body.expected_version = 2;
    assert.equal(validReturnRevisionReview(ledger, cancel, review), false);
});
test("retained sources must belong to the allocation recorded by the original return", () => {
    const { ledger, correct, review } = setupReturn(200);
    assert.ok(validReturnRevisionReview(ledger, correct, review));
    const last = review.capacities[0].lines.pop();
    const other = review.capacities[1];
    other.lines.pop();
    other.lines.push(last);
    assert.equal(validReturnRevisionReview(ledger, correct, review), false);
});
test("rejoining an original source requires its permanent parent, posting, document line and asset", () => {
    const { ledger, review, correct } = setupReturn(2);
    const { current, capacities } = review;
    current.members.pop();
    current.version = correct.body.expected_version = 2;
    current.revision_id = id(701);
    current.revision_reason = "Fictional earlier correction";
    const source = capacities[0].lines[1];
    source.returned_payment_amount = "0.03";
    source.remaining_payment_amount = "0.09";
    source.returned_document_amount = "0.00000003";
    source.remaining_document_amount = "0.00000009";
    assert.ok(validReturnRevisionReview(ledger, correct, review));
    for (const change of [
        (r) => {
            r.capacities[1].lines.push(r.capacities[0].lines.pop());
        },
        (r) => {
            r.capacities[0].lines[1].source_operation_id = id(702);
        },
        (r) => {
            r.capacities[0].lines[1].document_id = id(703);
        },
        (r) => {
            r.capacities[0].lines[1].document_line_id = id(704);
        },
        (r) => {
            r.assets.push({ asset_id: "ALT", scale: 8, enabled: true });
            r.capacities[0].lines[1].document_asset_id = "ALT";
        },
    ]) {
        const changed = structuredClone(review);
        change(changed);
        assert.equal(validReturnRevisionReview(ledger, correct, changed), false);
    }
});
