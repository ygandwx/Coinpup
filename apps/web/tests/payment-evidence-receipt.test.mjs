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
const { validOriginalPayment, sameOriginalPayment, validPaymentEvidenceTarget } =
    await import("../src/payment-evidence-receipt.ts");
const id = (n) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const ledger = id(2),
    payment = id(1);
function receipt(count = 200) {
    return {
        kind: "business_payment",
        id: payment,
        ledger_id: ledger,
        operation_id: id(3),
        journal_id: id(4),
        initial_allocation:
            count === 0
                ? null
                : {
                      kind: "business_allocation",
                      id: id(5),
                      ledger_id: ledger,
                      payment_id: payment,
                      postings: Array.from({ length: count }, (_, index) => ({
                          line_no: index + 1,
                          document_id: id(6),
                          document_line_id: id(20 + index),
                          operation_id: id(1000 + index),
                          journal_id: id(2000 + index),
                      })),
                  },
    };
}
test("complete null/one/200 original memberships and object-key order are preserved", () => {
    for (const count of [0, 1, 200]) {
        const original = receipt(count);
        const reordered = Object.fromEntries(Object.entries(structuredClone(original)).reverse());
        assert.equal(validOriginalPayment(original, ledger, payment), true);
        assert.equal(sameOriginalPayment(reordered, original, ledger, payment), true);
    }
});
test("a well-shaped partial receipt still cannot prove the frozen 200-member intent", () => {
    const original = receipt(),
        partial = structuredClone(original);
    partial.initial_allocation.postings.pop();
    assert.equal(validOriginalPayment(partial, ledger, payment), true);
    assert.equal(sameOriginalPayment(partial, original, ledger, payment), false);
});
test("foreign, malformed, extra, unordered or changed posting identities are rejected", () => {
    const original = receipt();
    for (const change of [
        (r) => {
            r.ledger_id = id(9);
        },
        (r) => {
            r.id = id(9);
        },
        (r) => {
            r.operation_id = id(9);
        },
        (r) => {
            r.journal_id = id(9);
        },
        (r) => {
            delete r.initial_allocation;
        },
        (r) => {
            r.operation = {};
        },
        (r) => {
            r.initial_allocation.postings.reverse();
        },
        (r) => {
            r.initial_allocation.postings[199].operation_id = id(9000);
        },
        (r) => {
            r.initial_allocation.postings[199].journal_id = id(9000);
        },
        (r) => {
            r.initial_allocation.postings[199].document_line_id = "fictional-invalid";
        },
        (r) => {
            r.initial_allocation.postings[199].document_line_id =
                r.initial_allocation.postings[0].document_line_id;
        },
        (r) => {
            r.initial_allocation.postings[0].line_no = "1";
        },
    ]) {
        const broken = structuredClone(original);
        change(broken);
        assert.equal(sameOriginalPayment(broken, original, ledger, payment), false);
    }
});

test("read metadata rejects numeric money and malformed identities before human review", () => {
    const value = {
        operation_status: "active",
        operation_version: 1,
        payment: {
            id: payment,
            ledger_id: ledger,
            receipt: receipt(),
            account_id: id(9),
            party_id: id(10),
            direction: "payout",
            asset_id: "USD",
            amount: "9007199254740993.01",
            applied_amount: "0",
            unapplied_amount: "9007199254740993.01",
            transaction_date: "2031-07-18",
            description: "Fictional metadata only",
            created_at: "2031-07-18T00:00:00Z",
            fees: [],
            fee_count: 0,
        },
    };
    const original = structuredClone(value);
    assert.equal(validPaymentEvidenceTarget(value, ledger, payment), true);
    assert.deepEqual(value, original);
    assert.equal(
        validPaymentEvidenceTarget(
            { ...value, operation_status: "cancelled", operation_version: 2 },
            ledger,
            payment,
        ),
        true,
    );
    for (const [key, broken] of Object.entries({
        amount: 10,
        applied_amount: "-1",
        account_id: "foreign",
        party_id: {},
        transaction_date: "2031-02-30",
        asset_id: "",
        fees: [{}],
        fee_count: 1,
        direction: "expense",
    })) {
        assert.equal(
            validPaymentEvidenceTarget(
                { ...value, payment: { ...value.payment, [key]: broken } },
                ledger,
                payment,
            ),
            false,
        );
    }
});
