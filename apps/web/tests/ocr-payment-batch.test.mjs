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
const { reviewedInput, reviewedPaymentInput } = await import("../src/ocr-batch.ts");
const id = (n) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
function setup() {
    const action = { kind: "link_payment", payment_id: id(1), expected_version: 4 };
    const review = {
        draft_id: id(3),
        version: 2,
        status: "draft",
        fields: [{ path: "rows.amount", requires_confirmation: true }],
        review: { confirmed: ["rows.amount"], entry: action },
    };
    const input = reviewedInput(id(2), id(4), review);
    const receipt = {
        kind: "business_payment",
        id: id(1),
        ledger_id: id(2),
        operation_id: id(5),
        journal_id: id(6),
        initial_allocation: {
            kind: "business_allocation",
            id: id(7),
            ledger_id: id(2),
            payment_id: id(1),
            postings: Array.from({ length: 200 }, (_, i) => ({
                line_no: i + 1,
                document_id: id(8),
                document_line_id: id(20 + i),
                operation_id: id(1000 + i),
                journal_id: id(2000 + i),
            })),
        },
    };
    const target = {
        operation_status: "active",
        operation_version: 4,
        payment: {
            id: id(1),
            ledger_id: id(2),
            receipt,
            account_id: id(9),
            party_id: id(10),
            direction: "payout",
            amount: "100",
            asset_id: "USD",
            applied_amount: "100",
            unapplied_amount: "0",
            transaction_date: "2031-07-18",
            created_at: "2031-07-18T00:00:00Z",
            description: "Fictional batch only",
            fees: [],
            fee_count: 0,
        },
    };
    return { review, input, target };
}
test("batch freezes the entire reviewed original payment without changing request intent", () => {
    const { input, target } = setup();
    const original = JSON.stringify(input.body);
    const prepared = reviewedPaymentInput(input, target);
    assert.equal(JSON.stringify(prepared.body), original);
    assert.equal(prepared.originalPayment.initial_allocation.postings.length, 200);
    target.payment.receipt.initial_allocation.postings.pop();
    assert.equal(prepared.originalPayment.initial_allocation.postings.length, 200);
    assert.equal(JSON.stringify(prepared.body), original);
});
test("missing, inactive, stale and foreign targets cannot prepare a batch payment link", () => {
    for (const change of [
        (v) => {
            v.operation_status = "cancelled";
        },
        (v) => {
            v.operation_version++;
        },
        (v) => {
            v.payment.ledger_id = id(99);
        },
        (v) => {
            v.payment.id = id(99);
        },
        (v) => {
            v.payment.amount = 100;
        },
        (v) => {
            v.payment.receipt.initial_allocation.postings[199].line_no = 199;
        },
    ]) {
        const { input, target } = setup();
        change(target);
        assert.throws(() => reviewedPaymentInput(input, target));
        assert.equal(input.originalPayment, undefined);
    }
});
test("unreviewed source fields still block batch preparation before payment lookup", () => {
    const { review } = setup();
    review.review.confirmed = [];
    assert.throws(() => reviewedInput(id(2), id(4), review), { code: "ocr_batch_not_ready" });
});
