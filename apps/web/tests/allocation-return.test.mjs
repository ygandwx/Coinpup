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
const { returnPaymentAmount } = await import("../src/allocation-return-fields.ts");
const { validReturnInput, validReturnReview } = await import("../src/allocation-return-intent.ts");
const { PendingBusinessSettlementController } =
    await import("../src/pending-business-settlement.ts");
const { ApiError } = await import("../src/api.ts");
const id = (n) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const ledger = id(1),
    session = { user: { id: "fictional-owner" }, csrf_token: "fictional-csrf" };
const source = (
    p = "110.00",
    d = "100.00",
    rp = "0.00",
    rd = "0.00",
    remainingP = p,
    remainingD = d,
) => ({
    source_allocation_line_id: id(3),
    source_operation_id: id(4),
    expected_version: 1,
    source_status: "active",
    document_id: id(5),
    document_line_id: id(6),
    document_asset_id: "EUR",
    description: "Fictional return 中文",
    payment_amount: p,
    document_amount: d,
    returned_payment_amount: rp,
    returned_document_amount: rd,
    remaining_payment_amount: remainingP,
    remaining_document_amount: remainingD,
});
function fixture() {
    const review = {
        capacity: {
            id: id(7),
            ledger_id: ledger,
            payment_id: id(8),
            asset_id: "USD",
            allocation_date: "2026-10-01",
            lines: [source()],
        },
        paymentScale: 2,
        documentScales: { EUR: 2 },
    };
    const input = {
        kind: "return",
        body: {
            id: id(9),
            payment_id: id(8),
            return_date: "2026-10-02",
            reason: "Fictional partial return",
            lines: [
                {
                    source_allocation_line_id: id(3),
                    expected_version: 1,
                    payment_amount: "22.00",
                    document_amount: "20",
                },
            ],
        },
    };
    return { input, review };
}
function receipt(plan) {
    const body = JSON.parse(plan.bodyJson),
        review = JSON.parse(plan.reviewedJson);
    return {
        kind: "business_allocation_return",
        id: body.id,
        ledger_id: plan.ledgerId,
        payment_id: body.payment_id,
        return_date: body.return_date,
        postings: body.lines.map((line, index) => {
            const s = review.capacity.lines.find(
                (row) => row.source_allocation_line_id === line.source_allocation_line_id,
            );
            return {
                line_no: index + 1,
                source_allocation_line_id: line.source_allocation_line_id,
                source_operation_id: s.source_operation_id,
                document_id: s.document_id,
                document_line_id: s.document_line_id,
                payment_amount: line.payment_amount.padEnd(21, "0"),
                document_amount: "20.000000000000000000",
                operation_id: id(30 + index),
                journal_id: id(40 + index),
            };
        }),
    };
}
function controller(io) {
    const c = new PendingBusinessSettlementController(io);
    c.setOwner(session.user.id);
    return c;
}

test("original-ratio minimum units and final residual are exact", () => {
    assert.equal(returnPaymentAmount(source(), "20", 2, 2), "22.00");
    assert.equal(returnPaymentAmount(source("0.03", "0.04"), "0.01", 2, 2), "0.01");
    const after = source("0.03", "0.04", "0.02", "0.02", "0.01", "0.02");
    assert.throws(() => returnPaymentAmount(after, "0.01", 2, 2), { code: "rounding" });
    assert.equal(returnPaymentAmount(after, "0.02", 2, 2), "0.01");
    assert.equal(
        returnPaymentAmount(
            source("99999999999999999999.99", "99999999999999999999.99"),
            "99999999999999999999.99",
            2,
            2,
        ),
        "99999999999999999999.99",
    );
    assert.throws(() => returnPaymentAmount(source("1", "3", "0", "0", "1", "3"), "2", 0, 0), {
        code: "rounding",
    });
    assert.equal(returnPaymentAmount(source("3", "4", "0", "0", "3", "4"), "2", 0, 0), "2");
});

test("zero, rounding loss, orphan residual and malformed capacity fail closed", () => {
    for (const value of ["0", "-1", "20.000", "1e2", "100.01"])
        assert.throws(() => returnPaymentAmount(source(), value, 2, 2));
    assert.throws(() => returnPaymentAmount(source("0.01", "100.00"), "1", 2, 2), {
        code: "rounding",
    });
    assert.throws(() => returnPaymentAmount(source("0.01", "100.00"), "60", 2, 2), {
        code: "rounding",
    });
    assert.throws(
        () => returnPaymentAmount(source("100", "100", "20", "20", "90", "80"), "10", 2, 2),
        { code: "source" },
    );
});

test("return review freezes scope, dates, versions, amount precision and original sources", () => {
    const { input, review } = fixture();
    assert(validReturnInput(input.body) && validReturnReview(ledger, input.body, review));
    for (const change of [
        (r) => (r.capacity.ledger_id = id(99)),
        (r) => (r.capacity.payment_id = id(99)),
        (r) => (r.capacity.lines[0].expected_version = 2),
        (r) => (r.capacity.lines[0].source_status = "cancelled"),
        (r) => (r.capacity.lines[0].source_operation_id = "invalid"),
        (r) => (r.capacity.lines = [null]),
        (r) => (r.capacity.allocation_date = "2026-10-03"),
        (r) => (r.documentScales.EUR = 0),
    ]) {
        const changed = structuredClone(review);
        change(changed);
        assert.equal(validReturnReview(ledger, input.body, changed), false);
    }
    for (const change of [
        (b) => (b.lines[0].payment_amount = "22.000"),
        (b) => (b.lines[0].payment_amount = "23"),
        (b) => (b.lines[0].document_amount = 20),
        (b) => b.lines.push({ ...b.lines[0] }),
        (b) => (b.reason = " "),
        (b) => (b.lines[0].expected_version = 0),
        (b) => (b.extra = "unexpected"),
    ]) {
        const changed = structuredClone(input.body);
        change(changed);
        assert.equal(validReturnReview(ledger, changed, review), false);
    }
});

test("receipt verifies original sources and numeric quantities while preserving 18-place text", async () => {
    const { input, review } = fixture();
    let observed;
    const c = controller(async (_, plan) => {
        observed = plan;
        return receipt(plan);
    });
    const flight = c.start(session, ledger, input, review);
    input.body.lines[0].document_amount = "100";
    review.capacity.lines[0].document_id = id(99);
    await flight;
    assert.equal(c.getSnapshot().status, "confirmed");
    assert.equal(JSON.parse(observed.bodyJson).lines[0].document_amount, "20");
    assert.equal(c.getSnapshot().receipt.postings[0].document_id, id(5));
    assert.equal(c.getSnapshot().receipt.postings[0].payment_amount, "22.000000000000000000");
    assert(Object.isFrozen(c.getSnapshot().receipt.postings[0]));
});

for (const field of [
    "source_allocation_line_id",
    "source_operation_id",
    "document_id",
    "document_line_id",
    "payment_amount",
    "document_amount",
    "operation_id",
    "line_no",
])
    test(`corrupt return ${field} stays unknown`, async () => {
        const { input, review } = fixture();
        const c = controller(async (_, plan) => {
            const result = receipt(plan);
            result.postings[0][field] = field.endsWith("amount")
                ? "19"
                : field === "line_no"
                  ? 2
                  : field === "operation_id"
                    ? id(4)
                    : id(99);
            return result;
        });
        await c.start(session, ledger, input, review);
        assert.equal(c.getSnapshot().status, "unknown");
        assert.equal(c.dismiss(), false);
    });

test("duplicate, lost response and same-owner login retry retain original intent and key", async () => {
    const { input, review } = fixture();
    const calls = [];
    let resolve;
    const c = controller(async (s, p) => {
        calls.push([s, p]);
        if (calls.length === 1) return await new Promise((_, reject) => (resolve = reject));
        return receipt(p);
    });
    const first = c.start(session, ledger, input, review);
    await Promise.resolve();
    await assert.rejects(c.start(session, ledger, input, review));
    resolve(new ApiError("network"));
    await first;
    assert.equal(c.getSnapshot().status, "unknown");
    c.setOwner(null);
    assert.equal(c.getSnapshot().plan, null);
    c.setOwner(session.user.id);
    const fresh = { ...session, csrf_token: "fictional-fresh" };
    await c.retry(fresh);
    assert.equal(c.getSnapshot().status, "confirmed");
    assert.equal(calls.length, 2);
    assert.equal(calls[0][1], calls[1][1]);
    assert.equal(calls[1][0].csrf_token, "fictional-fresh");
});

test("401 waits for explicit retry; owner change discards a late return response", async () => {
    const { input, review } = fixture();
    const c = controller(async () => {
        throw new ApiError("unauthorized", 401);
    });
    await c.start(session, ledger, input, review);
    assert.equal(c.getSnapshot().status, "auth-required");
    let finish;
    const d = controller(
        async (_, plan) => await new Promise((resolve) => (finish = () => resolve(receipt(plan)))),
    );
    const flight = d.start(session, ledger, input, review);
    await Promise.resolve();
    d.setOwner("fictional-other");
    finish();
    await flight;
    assert.equal(d.getSnapshot().plan, null);
    assert.equal(d.getSnapshot().receipt, null);
});

test("missing review, definite rejection, unknown later conflict and explicit logout", async () => {
    const { input, review } = fixture();
    let calls = 0;
    const c = controller(async () => {
        calls++;
        throw new ApiError("server", 409, "period_closed");
    });
    await assert.rejects(c.start(session, ledger, input));
    assert.equal(calls, 0);
    await c.start(session, ledger, input, review);
    assert.equal(c.getSnapshot().status, "rejected");
    assert(c.dismiss());
    let attempts = 0;
    const d = controller(async () => {
        throw ++attempts === 1
            ? new ApiError("server", 503)
            : new ApiError("server", 409, "period_closed");
    });
    await d.start(session, ledger, input, review);
    assert.equal(d.getSnapshot().status, "unknown");
    await d.retry(session);
    assert.equal(d.getSnapshot().status, "unknown");
    assert.equal(d.dismiss(), false);
    d.setOwner(null, true);
    assert.equal(d.getSnapshot().plan, null);
});

test("multi-line receipt requires every ordered member and distinct new postings", async () => {
    const { input, review } = fixture();
    input.body.lines.push({ ...input.body.lines[0], source_allocation_line_id: id(13) });
    review.capacity.lines.push({
        ...source(),
        source_allocation_line_id: id(13),
        source_operation_id: id(14),
        document_line_id: id(16),
    });
    const valid = controller(async (_, plan) => receipt(plan));
    await valid.start(session, ledger, input, review);
    assert.equal(valid.getSnapshot().status, "confirmed");
    for (const corrupt of [
        (r) => r.postings.pop(),
        (r) => r.postings.reverse(),
        (r) => (r.postings[1].operation_id = r.postings[0].operation_id),
        (r) => (r.postings[1].journal_id = r.postings[0].journal_id),
        (r) => (r.postings[0].operation_id = id(14)),
        (r) => (r.payment_id = id(99)),
        (r) => (r.ledger_id = id(99)),
        (r) => (r.return_date = "2026-10-03"),
        (r) => (r.postings[0].extra = "unexpected"),
    ]) {
        const c = controller(async (_, plan) => {
            const result = receipt(plan);
            corrupt(result);
            return result;
        });
        await c.start(session, ledger, input, review);
        assert.equal(c.getSnapshot().status, "unknown");
    }
});
