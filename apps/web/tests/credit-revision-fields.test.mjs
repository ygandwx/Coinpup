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
const fields = await import("../src/credit-revision-fields.ts");
const { validCreditRevisionReview } = await import("../src/credit-revision-intent.ts");
import { fixture, cancelled, ledger, creditId, id } from "./credit-revision-fixture.mjs";
const valid = (draft, review) =>
    validCreditRevisionReview(ledger, creditId, draft.action, draft.body, review);

test("initial correction copies the complete current group, never the document version or prior revision reason", () => {
    const { review } = fixture();
    review.current.credit_reason = "\u0085 Fictional fact 😀 \ufeff";
    review.current.members[0].net_amount = "17.2";
    review.current.members[0].tax_amount = "2.8";
    const original = JSON.stringify(review);
    const draft = fields.initialCreditRevision("correct", review);
    assert.equal(draft.body.expected_version, 3);
    assert.equal(review.confirmation.document_version, 2);
    assert.equal(draft.body.reason, "");
    assert.equal(draft.body.replacement.reason, review.current.credit_reason);
    assert.equal(draft.body.replacement.credit_date, review.current.credit_date);
    assert.deepEqual(
        draft.body.replacement.lines,
        review.current.members.map((row) => ({
            source_line_id: row.source_line_id,
            net_amount: row.net_amount,
            tax_amount: row.tax_amount,
            recognition_date: row.recognition_date,
        })),
    );
    assert(!valid(draft, review)); // A new, explicit correction reason is still required.
    const explained = fields.setCreditRevisionReason(draft, " Fictional correction ");
    assert(valid(explained, review));
    assert.equal(draft.body.reason, "");
    explained.body.replacement.lines[0].net_amount = "1.00";
    assert.equal(JSON.stringify(review), original);
});

test("complete replacement edits preserve order and strings; new sources never infer net or tax", () => {
    const { review } = fixture();
    let draft = fields.setCreditRevisionReason(
        fields.initialCreditRevision("correct", review),
        "Fictional correction",
    );
    const original = JSON.stringify(draft);
    const unchanged = fields.selectCreditRevisionLine(draft, review, id(101), true);
    assert.equal(JSON.stringify(unchanged), original);
    draft = fields.selectCreditRevisionLine(draft, review, id(101), false);
    draft = fields.selectCreditRevisionLine(draft, review, id(103), true);
    assert.deepEqual(
        draft.body.replacement.lines.map((row) => row.source_line_id),
        [id(102), id(103)],
    );
    assert.deepEqual(draft.body.replacement.lines[1], {
        source_line_id: id(103),
        net_amount: "",
        tax_amount: "",
        recognition_date: review.current.credit_date,
    });
    assert(!valid(draft, review));
    draft = fields.updateCreditCorrection(draft, (body) => ({
        ...body,
        replacement: {
            ...body.replacement,
            lines: body.replacement.lines.map((row) =>
                row.source_line_id === id(103)
                    ? { ...row, net_amount: "0", tax_amount: "3.0", recognition_date: "2026-09-01" }
                    : row,
            ),
        },
    }));
    assert(valid(draft, review)); // Recognition date is explicit, not bounded by issue date.
    const beforeReselect = JSON.stringify(draft);
    const reselected = fields.selectCreditRevisionLine(draft, review, id(101), true);
    assert.deepEqual(
        reselected.body.replacement.lines.map((row) => row.source_line_id),
        [id(102), id(103), id(101)],
    );
    assert.equal(reselected.body.replacement.lines[2].net_amount, "17.23");
    assert.equal(reselected.body.replacement.lines[2].recognition_date, "2026-11-01");
    assert.equal(JSON.stringify(draft), beforeReselect);
    assert.equal(fields.selectCreditRevisionLine(draft, review, id(999), true), draft);
    for (const source of review.capacity.lines)
        draft = fields.selectCreditRevisionLine(draft, review, source.source_line_id, false);
    assert.deepEqual(draft.body.replacement.lines, []);
    assert(!valid(draft, review)); // Empty correction must not silently become cancellation.
});

test("cancellation initialization needs no capacity or enabled asset and cannot gain replacement fields", () => {
    const { review } = cancelled();
    const original = JSON.stringify(review);
    const draft = fields.setCreditRevisionReason(
        fields.initialCreditRevision("cancel", review),
        "\u0085 Fictional cancel ",
    );
    assert.deepEqual(draft.body, { expected_version: 3, reason: "\u0085 Fictional cancel " });
    assert(valid(draft, review));
    assert(fields.canSubmitCreditRevision(ledger, creditId, draft, review));
    assert.equal(
        fields.updateCreditCorrection(draft, () => {
            throw new Error("Must not edit cancellation");
        }),
        draft,
    );
    assert.equal(fields.selectCreditRevisionLine(draft, review, id(103), true), draft);
    assert.equal(fields.creditRevisionLimits(review, id(101)), null);
    assert.equal(JSON.stringify(review), original);
    review.current.status = "cancelled";
    assert.throws(() => fields.initialCreditRevision("correct", review));
    assert.throws(() => fields.initialCreditRevision("cancel", review));
});

test("display limits restore only this group's net, tax and gross; invalid snapshots cannot submit", () => {
    const { review } = fixture();
    assert.deepEqual(fields.creditRevisionLimits(review, id(101)), {
        net: "97.23",
        tax: "8.77",
        gross: "56.00",
    });
    assert.deepEqual(fields.creditRevisionLimits(review, id(103)), {
        net: "100.00",
        tax: "10.00",
        gross: "110.00",
    });
    const draft = fields.setCreditRevisionReason(
        fields.initialCreditRevision("correct", review),
        "Fictional correction",
    );
    assert(valid(draft, review));
    for (const invalid of ["not-a-number", "1.001", "-0", "-1", "100000000000000000000", 0, null]) {
        const bad = structuredClone(review);
        bad.capacity.lines[0].available_gross_amount = invalid;
        assert.equal(fields.creditRevisionLimits(bad, id(101)), null);
        assert(!valid(draft, bad));
    }
    const inconsistent = structuredClone(review);
    inconsistent.capacity.lines[0].available_gross_amount = "35.00";
    assert(!valid(draft, inconsistent)); // A display preview never overrides shared validation.
});

test("safe bilingual amount display preserves long exact quantities and reports malformed source data", () => {
    const { review } = fixture();
    const text = "99999999999999999999.99";
    for (const locale of ["zh", "en"]) {
        assert.equal(
            fields.formatCreditRevisionAmount(text, review.asset, locale),
            "99,999,999,999,999,999,999.99 USD",
        );
        for (const invalid of ["broken", "0e0", "-0", "1.001", 1, null])
            assert.equal(fields.formatCreditRevisionAmount(invalid, review.asset, locale), null);
    }
    const eth = {
        ...review.asset,
        asset_id: "ETH",
        code: "ETH",
        kind: "native",
        network: "ethereum",
        scale: 18,
    };
    assert.equal(
        fields.formatCreditRevisionAmount("0.000000000000000001", eth, "en"),
        "0.000000000000000001 ETH",
    );
    assert.equal(fields.formatCreditRevisionAmount("100000000000000000000", eth, "en"), null);
    assert.equal(fields.formatCreditRevisionAmount("1", { ...eth, scale: 19 }, "en"), null);
});

test("a numerically consistent but malformed capacity preview cannot submit a correction", () => {
    const { review } = fixture();
    const draft = fields.setCreditRevisionReason(
        fields.initialCreditRevision("correct", review),
        "Fictional correction",
    );
    review.capacity.lines[0].applied_amount = "86.00";
    review.capacity.lines[0].available_gross_amount = "0";
    assert(fields.canSubmitCreditRevision(ledger, creditId, draft, review));
    review.capacity.lines[0].available_gross_amount = "-0";
    const original = JSON.stringify(review);
    assert(valid(draft, review)); // Existing quantity comparison treats signed zero numerically.
    assert.equal(fields.creditRevisionLimits(review, id(101)), null);
    assert(!fields.canSubmitCreditRevision(ledger, creditId, draft, review));
    assert.equal(JSON.stringify(review), original);
});
