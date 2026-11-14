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
const { validRefundRevisionInput, validRefundRevisionReview, validFrozenRefundRevision } =
    await import("../src/refund-revision-intent.ts");
const { id, refundRevision, frozen } = await import("./refund-revision-fixture.mjs");
const valid = (f) => validRefundRevisionReview(f.scope.ledger_id, f.input, f.review);
test("a correction can reclaim its own current principal, and only that amount", () => {
    const f = refundRevision();
    assert(valid(f));
    f.input.body.replacement.payment_amount = "25.01";
    assert(!valid(f));
    f.input.body.replacement.payment_amount = "25.00";
    f.review.cash.capacity.refunded_amount = "19.99";
    f.review.cash.capacity.refundable_amount = "20.01";
    assert(!valid(f));
    const largest = refundRevision();
    const max = "9".repeat(20) + ".99";
    Object.assign(largest.review.cash.capacity, {
        amount: max,
        applied_amount: "0.00",
        refunded_amount: "20.00",
        refundable_amount: "9".repeat(18) + "79.99",
    });
    largest.input.body.replacement.payment_amount = max;
    assert(valid(largest));
    largest.input.body.replacement.payment_amount = "1" + "0".repeat(20);
    assert(!valid(largest));
});
test("replacement inputs bind ordered source evidence, actual assets, references and dates", () => {
    for (const change of [
        (f) => (f.review.cash.accounts[0].archived = true),
        (f) => (f.review.cash.categories[0].ledger_id = id(50)),
        (f) => (f.review.cash.assets[1].enabled = false),
        (f) => (f.input.body.replacement.allocation_return_ids = [id(60)]),
        (f) => (f.input.body.replacement.allocation_return_ids = [id(6), id(6)]),
        (f) => (f.input.body.replacement.fees[0].amount = "0.000000001"),
        (f) => (f.input.body.replacement.transaction_date = "2026-10-01"),
        (f) => (f.review.cash.capacity.party_id = id(66)),
        (f) => (f.review.original.payment_id = id(60)),
        (f) => (f.review.current.status = "cancelled"),
        (f) => (f.input.body.expected_version = 2),
        (f) => (f.review.original.receipt.journal_id = id(99)),
        (f) => (f.review.current.fact.amount = "19.96"),
        (f) => (f.input.body.replacement.payment_id = id(99)),
        (f) => (f.input.body.replacement.fees = null),
        (f) => (f.input.body.replacement.payment_amount = 25),
    ]) {
        const f = refundRevision();
        change(f);
        assert(!valid(f), String(change));
    }
});
test("correction reason and original fact reason follow distinct Unicode length rules", () => {
    const f = refundRevision();
    f.input.body.reason = "\u0085" + "🪙".repeat(1000) + "\u001d";
    f.input.body.replacement.reason = "🪙".repeat(2000);
    assert(valid(f));
    f.input.body.replacement.reason += " ";
    assert(!validRefundRevisionInput(f.input));
    f.input.body.replacement.reason = "\u001c";
    assert(!validRefundRevisionInput(f.input));
    f.input.body.replacement.reason = "\ufeff";
    assert(validRefundRevisionInput(f.input));
    f.input.body.reason += "x";
    assert(!validRefundRevisionInput(f.input));
    for (const v of [false, "1", 1.5, 0, 2147483647]) {
        const g = refundRevision();
        g.input.body.expected_version = v;
        assert(!validRefundRevisionInput(g.input));
    }
});
test("cancellation of a disabled or archived old cash reference needs only historical precision", () => {
    const f = refundRevision("cancel");
    delete f.review.cash;
    f.review.assets[1].enabled = false;
    assert(valid(f));
    assert(validFrozenRefundRevision(f.result, frozen(f)));
    const stale = structuredClone(f);
    stale.input.body.replacement = null;
    assert(!valid(stale));
});
test("only the exact fixed 200 with both ordered immutable facts proves a frozen command", () => {
    const f = refundRevision();
    const plan = frozen(f);
    f.result.previous.amount = "19.970000000000000000";
    f.result.replacement.payment_amount = "25.000000000000000000";
    assert(validFrozenRefundRevision(f.result, plan));
    for (const change of [
        (r) => (r.ledger_id = id(80)),
        (r) => (r.refund_id = id(80)),
        (r) => (r.previous_journal_id = id(80)),
        (r) => (r.operation_id = id(80)),
        (r) => (r.action = "cancel"),
        (r) => (r.previous_version = 2),
        (r) => (r.replacement.amount = "22.21"),
        (r) => (r.previous.reason = "different"),
        (r) => (r.replacement.reason = "different"),
        (r) => (r.previous.allocation_return_ids = []),
        (r) => r.replacement.fees.reverse(),
        (r) => (r.replacement.fees[1].category_id = id(51)),
        (r) => (r.replacement.fees[0].amount = "0.00000002"),
        (r) => (r.replacement_journal_id = r.reversal_journal_id),
        (r) => (r.replacement.allocation_return_ids = [id(6)]),
        (r) => (r.replacement.account_id = id(62)),
        (r) => (r.replacement.source_version = 2),
    ]) {
        const altered = structuredClone(f.result);
        change(altered);
        assert(!validFrozenRefundRevision(altered, plan), String(change));
    }
    assert(!validFrozenRefundRevision(f.result, { ...plan, reviewedJson: "{" }));
});
