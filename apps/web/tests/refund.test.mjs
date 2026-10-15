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
const { validRefundInput, validRefundReview, validRefundReceipt } =
    await import("../src/refund-intent.ts");
const { PendingBusinessSettlementController } =
    await import("../src/pending-business-settlement.ts");
const { ApiError } = await import("../src/api.ts");
const id = (n) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const ledger = id(1),
    session = { user: { id: "fictional-owner" }, csrf_token: "fictional-csrf" };
function fixture() {
    const input = {
        kind: "refund",
        body: {
            id: id(10),
            payment_id: id(2),
            expected_version: 1,
            account_id: id(4),
            asset_id: "EUR",
            amount: "19.37",
            payment_amount: "20",
            transaction_date: "2026-10-02",
            reason: "Fictional refund 中文",
            allocation_return_ids: [id(7)],
            fees: [{ account_id: id(4), asset_id: "USD", category_id: id(5), amount: "0.25" }],
        },
    };
    const review = {
        capacity: {
            id: id(2),
            ledger_id: ledger,
            party_id: id(3),
            payment_direction: "payout",
            asset_id: "USD",
            transaction_date: "2026-09-15",
            expected_version: 1,
            source_status: "active",
            amount: "100.00",
            applied_amount: "80.00",
            refunded_amount: "0.00",
            refundable_amount: "20.00",
        },
        paymentOperationId: id(8),
        paymentJournalId: id(9),
        accounts: [
            {
                id: id(4),
                ledger_id: ledger,
                kind: "bank",
                archived: false,
                asset_ids: ["USD", "EUR", "BTC"],
            },
        ],
        assets: [
            { asset_id: "USD", enabled: true, scale: 2 },
            { asset_id: "EUR", enabled: true, scale: 2 },
            { asset_id: "BTC", enabled: true, scale: 8 },
        ],
        categories: [{ id: id(5), ledger_id: ledger, kind: "expense", archived: false }],
        returns: [{ id: id(7), ledger_id: ledger, payment_id: id(2), return_date: "2026-10-01" }],
    };
    const receipt = {
        kind: "business_refund",
        id: id(10),
        ledger_id: ledger,
        payment_id: id(2),
        operation_id: id(11),
        journal_id: id(12),
        account_id: id(4),
        asset_id: "EUR",
        amount: "19.370000000000000000",
        payment_asset_id: "USD",
        payment_amount: "20.000000000000000000",
        transaction_date: "2026-10-02",
        allocation_return_ids: [id(7)],
        fees: [
            {
                account_id: id(4),
                asset_id: "USD",
                category_id: id(5),
                amount: "0.250000000000000000",
            },
        ],
    };
    return { input, review, receipt };
}
function plan(f) {
    return {
        ledgerId: ledger,
        kind: "refund",
        bodyJson: JSON.stringify(f.input.body),
        reviewedJson: JSON.stringify(f.review),
    };
}
function controller(io) {
    const result = new PendingBusinessSettlementController(io);
    result.setOwner(session.user.id);
    return result;
}

test("refund accepts explicit independent assets, exact principal and separate fees", () => {
    const f = fixture();
    assert(validRefundInput(f.input.body));
    assert(validRefundReview(ledger, f.input.body, f.review));
    assert(validRefundReceipt(f.receipt, plan(f)));
    f.input.body.asset_id = "USD";
    assert.equal(validRefundReview(ledger, f.input.body, f.review), false);
    f.input.body.amount = "20.00";
    assert(validRefundReview(ledger, f.input.body, f.review));
});

test("refund rejects malformed, over-range or ambiguous input without numeric coercion", () => {
    for (const change of [
        { amount: 19.37 },
        { amount: "0" },
        { amount: "-1" },
        { amount: "1e2" },
        { payment_amount: "100000000000000000000" },
        { transaction_date: "2026-02-30" },
        { reason: " " },
        { id: "not-a-uuid" },
        { expected_version: 1.5 },
        { expected_version: 0 },
        { fees: null },
        { allocation_return_ids: null },
        { allocation_return_ids: [id(7), id(7)] },
        { unexpected: true },
    ]) {
        const f = fixture();
        assert.equal(
            validRefundInput({ ...f.input.body, ...change }),
            false,
            JSON.stringify(change),
        );
    }
});

test("refund review rejects inconsistent source capacity, disabled assets and foreign references", () => {
    for (const mutate of [
        (f) => (f.review.capacity.ledger_id = id(90)),
        (f) => (f.review.capacity.expected_version = 2),
        (f) => (f.review.capacity.source_status = "cancelled"),
        (f) => (f.review.capacity.refunded_amount = "1"),
        (f) => (f.review.capacity.refundable_amount = "21"),
        (f) => (f.review.assets[1].enabled = false),
        (f) => (f.review.accounts[0].archived = true),
        (f) => (f.review.accounts[0].ledger_id = id(90)),
        (f) => (f.review.accounts[0].kind = "control"),
        (f) => (f.review.accounts[0].asset_ids = ["USD"]),
        (f) => (f.review.categories[0].kind = "income"),
        (f) => (f.review.categories[0].archived = true),
        (f) => (f.review.returns[0].payment_id = id(90)),
        (f) => (f.review.returns[0].return_date = "2026-10-03"),
        (f) => (f.input.body.transaction_date = "2026-09-14"),
        (f) => (f.input.body.amount = "19.371"),
        (f) => (f.input.body.payment_amount = "20.01"),
        (f) => (f.input.body.fees[0].amount = "0.251"),
        (f) => f.review.accounts.push(f.review.accounts[0]),
        (f) => (f.review.assets[0].scale = 19),
    ]) {
        const f = fixture();
        mutate(f);
        assert.equal(validRefundReview(ledger, f.input.body, f.review), false, String(mutate));
    }
});

test("maximum principal and minimum actual crypto remain exact without floating point", () => {
    const f = fixture();
    Object.assign(f.review.capacity, {
        amount: "99999999999999999999.99",
        applied_amount: "0.00",
        refundable_amount: "99999999999999999999.99",
    });
    Object.assign(f.input.body, {
        asset_id: "BTC",
        amount: "0.00000001",
        payment_amount: "99999999999999999999.99",
        fees: [],
        allocation_return_ids: [],
    });
    assert(validRefundReview(ledger, f.input.body, f.review));
    f.input.body.payment_amount = "99999999999999999999.991";
    assert.equal(validRefundReview(ledger, f.input.body, f.review), false);
});

test("receipt must identify every actual quantity, original source, fee and independent posting", () => {
    for (const mutate of [
        (r) => (r.id = id(90)),
        (r) => (r.ledger_id = id(90)),
        (r) => (r.payment_id = id(90)),
        (r) => (r.account_id = id(90)),
        (r) => (r.asset_id = "USD"),
        (r) => (r.amount = "20"),
        (r) => (r.payment_asset_id = "EUR"),
        (r) => (r.payment_amount = "19.37"),
        (r) => (r.transaction_date = "2026-10-03"),
        (r) => (r.operation_id = id(8)),
        (r) => (r.journal_id = id(9)),
        (r) => (r.allocation_return_ids = []),
        (r) => (r.fees = []),
        (r) => (r.fees[0].amount = "0.24"),
        (r) => (r.fees[0].account_id = id(90)),
        (r) => (r.fees[0].category_id = id(90)),
        (r) => (r.fees[0].asset_id = "EUR"),
        (r) => (r.extra = true),
        (r) => (r.operation_id = null),
    ]) {
        const f = fixture();
        mutate(f.receipt);
        assert.equal(validRefundReceipt(f.receipt, plan(f)), false, String(mutate));
    }
});

test("double submit freezes one refund and deep-freezes the verified receipt", async () => {
    const f = fixture();
    let resolve,
        calls = 0;
    const c = controller(() => {
        calls += 1;
        return new Promise((done) => (resolve = done));
    });
    const first = c.start(session, ledger, f.input, f.review);
    await assert.rejects(
        c.start(session, ledger, f.input, f.review),
        /pending_settlement_unresolved/u,
    );
    assert.equal(calls, 1);
    resolve(f.receipt);
    await first;
    assert.equal(c.getSnapshot().status, "confirmed");
    assert(Object.isFrozen(c.getSnapshot().receipt.fees[0]));
    assert(c.dismiss());
});

test("lost response and expired session retry the identical body key and frozen review", async () => {
    const f = fixture(),
        requests = [];
    const c = controller(async (_session, frozen) => {
        requests.push(frozen);
        if (requests.length === 1) throw new ApiError("network", null, "network");
        if (requests.length === 2) throw new ApiError("unauthorized", 401, "session_expired");
        return f.receipt;
    });
    await c.start(session, ledger, f.input, f.review);
    assert.equal(c.getSnapshot().status, "unknown");
    assert.equal(c.dismiss(), false);
    f.input.body.amount = "99";
    f.review.capacity.refundable_amount = "99";
    await c.retry(session);
    c.setOwner(null);
    assert.equal(c.getSnapshot().plan, null);
    c.setOwner(session.user.id);
    await c.retry(session);
    assert.equal(c.getSnapshot().status, "confirmed");
    assert.equal(new Set(requests.map((p) => p.key)).size, 1);
    assert.equal(new Set(requests.map((p) => p.bodyJson)).size, 1);
    assert.equal(new Set(requests.map((p) => p.reviewedJson)).size, 1);
});

test("a different owner clears the refund and ignores a late original success", async () => {
    const f = fixture();
    let resolve;
    const c = controller(() => new Promise((done) => (resolve = done)));
    const first = c.start(session, ledger, f.input, f.review);
    await Promise.resolve(); // The transport must start before the owner changes.
    assert.equal(typeof resolve, "function");
    c.setOwner("fictional-other");
    resolve(f.receipt);
    await first;
    assert.equal(c.getSnapshot().plan, null);
    assert.equal(c.getSnapshot().status, "idle");
});

test("malformed receipt and a later capacity rejection never prove an unknown refund failed", async () => {
    const f = fixture();
    let calls = 0;
    const c = controller(async () => {
        calls += 1;
        if (calls === 1) return { ...f.receipt, payment_amount: "19" };
        throw new ApiError("validation", 422, "refund_exceeds_payment");
    });
    await c.start(session, ledger, f.input, f.review);
    assert.equal(c.getSnapshot().status, "unknown");
    await c.retry(session);
    assert.equal(c.getSnapshot().status, "unknown");
    assert.equal(c.dismiss(), false);
});
