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
const { validCreditInput, validCreditReview, validCreditReceipt } =
    await import("../src/credit-intent.ts");
const { PendingBusinessCreditController } = await import("../src/pending-business-credit.ts");
const { ApiError } = await import("../src/api.ts");
const id = (n) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const ledger = id(1),
    session = { user: { id: "fictional-owner" }, csrf_token: "fictional-token" };
function fixture() {
    const body = {
        id: id(2),
        document_id: id(3),
        expected_version: 2,
        credit_date: "2026-10-21",
        reason: "Fictional credit 中文",
        lines: [
            {
                source_line_id: id(4),
                net_amount: "17.23",
                tax_amount: "2.77",
                recognition_date: "2026-10-20",
            },
        ],
    };
    const review = {
        asset: {
            asset_id: "USD",
            kind: "fiat",
            scale: 2,
            enabled: true,
            network: null,
            token_reference: null,
        },
        confirmation: {
            kind: "business_confirmation",
            id: id(5),
            ledger_id: ledger,
            document_id: id(3),
            source_version: 1,
            document_version: 2,
            postings: [{ line_id: id(4), line_no: 1, operation_id: id(6), journal_id: id(7) }],
        },
        capacity: {
            id: id(3),
            ledger_id: ledger,
            document_kind: "invoice",
            party_id: id(8),
            asset_id: "USD",
            issue_date: "2026-09-30",
            expected_version: 2,
            lines: [
                {
                    source_line_id: id(4),
                    category_id: id(9),
                    project_id: null,
                    description: "Fictional source",
                    recognition_date: "2026-09-30",
                    net_amount: "100.00",
                    tax_amount: "10.00",
                    total_amount: "110.00",
                    credited_net_amount: "0.00",
                    credited_tax_amount: "0.00",
                    applied_amount: "90.00",
                    remaining_net_amount: "100.00",
                    remaining_tax_amount: "10.00",
                    available_gross_amount: "20.00",
                },
            ],
        },
    };
    return { body, review };
}
function receipt(plan) {
    const body = JSON.parse(plan.bodyJson),
        review = JSON.parse(plan.reviewedJson);
    return {
        kind: "business_credit",
        id: body.id,
        ledger_id: ledger,
        document_id: body.document_id,
        source_version: body.expected_version,
        asset_id: review.asset.asset_id,
        credit_date: body.credit_date,
        postings: body.lines.map((line, index) => ({
            ...line,
            line_no: index + 1,
            source_operation_id: review.confirmation.postings.find(
                (row) => row.line_id === line.source_line_id,
            ).operation_id,
            net_amount: line.net_amount,
            tax_amount: line.tax_amount,
            total_amount: "20.000000000000000000",
            operation_id: id(1000 + index),
            journal_id: id(2000 + index),
        })),
    };
}
const plan = (f) => ({
    ownerId: session.user.id,
    ledgerId: ledger,
    key: "fictional-key",
    bodyJson: JSON.stringify(f.body),
    reviewedJson: JSON.stringify(f.review),
});

test("manual credit review preserves independent net/tax, original source and exact storage padding", () => {
    const f = fixture(),
        frozen = plan(f),
        result = receipt(frozen);
    assert(validCreditInput(f.body));
    assert(validCreditReview(ledger, f.body, f.review));
    result.postings[0].net_amount = "17.230000000000000000";
    result.postings[0].tax_amount = "2.770000000000000000";
    assert(validCreditReceipt(result, frozen));
    for (const mutate of [
        (f) => (f.body.lines[0].net_amount = "17.230"),
        (f) => (f.body.lines[0].tax_amount = "-1"),
        (f) => (f.body.lines[0].tax_amount = "10.01"),
        (f) => (f.body.lines[0].net_amount = "18"),
        (f) => (f.body.credit_date = "2026-09-29"),
        (f) => (f.body.lines[0].recognition_date = "2026-02-30"),
        (f) => f.body.lines.push({ ...f.body.lines[0] }),
        (f) => (f.body.lines[0].net_amount = 17.23),
        (f) => (f.review.capacity.ledger_id = id(999)),
        (f) => (f.review.capacity.lines[0].available_gross_amount = "21"),
        (f) => (f.review.capacity.lines[0].remaining_net_amount = "99"),
        (f) => (f.review.capacity.lines[0] = null),
        (f) => (f.review.confirmation.postings[0].line_id = id(999)),
        (f) => (f.review.confirmation.document_version = 3),
        (f) => (f.review.asset.enabled = false),
        (f) => (f.review.asset.scale = 19),
    ]) {
        const bad = structuredClone(f);
        mutate(bad);
        assert.equal(validCreditReview(ledger, bad.body, bad.review), false);
    }
    for (const mutate of [
        (r) => (r.postings[0].source_operation_id = id(99)),
        (r) => (r.postings[0].operation_id = id(6)),
        (r) => (r.postings[0].journal_id = id(7)),
        (r) => (r.postings[0].net_amount = "17.24"),
        (r) => (r.postings[0].tax_amount = "2.76"),
        (r) => (r.postings[0].recognition_date = "2026-10-21"),
        (r) => (r.postings = []),
        (r) => (r.postings[0].extra = "unexpected"),
        (r) => (r.ledger_id = id(99)),
    ]) {
        const bad = structuredClone(result);
        mutate(bad);
        assert.equal(validCreditReceipt(bad, frozen), false);
    }
});

test("unknown credit keeps original JSON/key after changed capacity, double click and a 401 response", async () => {
    const f = fixture(),
        calls = [];
    const controller = new PendingBusinessCreditController(async (auth, frozen) => {
        calls.push({ ...frozen, csrf: auth.csrf_token });
        if (calls.length === 1) throw new ApiError("network");
        if (calls.length === 2) throw new ApiError("server", 409, "credit_exceeds_open");
        if (calls.length === 3) throw new ApiError("unauthorized", 401);
        return receipt(frozen);
    });
    controller.setOwner(session.user.id);
    const started = controller.start(session, ledger, f.body, f.review);
    await assert.rejects(controller.start(session, ledger, f.body, f.review));
    await started;
    assert.equal(controller.getSnapshot().status, "unknown");
    const original = controller.getSnapshot().plan;
    f.body.lines[0].net_amount = "99";
    f.review.capacity.lines[0].available_gross_amount = "0";
    await controller.retry(session);
    assert.equal(controller.getSnapshot().status, "unknown");
    assert.equal(controller.dismiss(), false);
    await controller.retry(session);
    assert.equal(controller.getSnapshot().status, "auth-required");
    controller.setOwner(null);
    assert.equal(controller.getSnapshot().plan, null);
    controller.setOwner(session.user.id);
    await controller.retry({ ...session, csrf_token: "fictional-fresh" });
    assert.equal(controller.getSnapshot().status, "confirmed");
    assert(calls.every((call) => call.bodyJson === original.bodyJson && call.key === original.key));
    assert.equal(calls.at(-1).csrf, "fictional-fresh");
    const saved = controller.getSnapshot().receipt;
    assert(Object.isFrozen(saved.postings) && Object.isFrozen(saved.postings[0]));
});

test("credit groups support 200 exact sources and maximum-sized quantities without Number", () => {
    const f = fixture(),
        line = f.body.lines[0],
        source = f.review.capacity.lines[0];
    f.body.lines = Array.from({ length: 200 }, (_, i) => ({
        ...line,
        source_line_id: id(3000 + i),
    }));
    f.review.capacity.lines = f.body.lines.map((row) => ({
        ...source,
        source_line_id: row.source_line_id,
    }));
    f.review.confirmation.postings = f.body.lines.map((row, i) => ({
        line_id: row.source_line_id,
        line_no: i + 1,
        operation_id: id(4000 + i),
        journal_id: id(5000 + i),
    }));
    assert(validCreditReview(ledger, f.body, f.review));
    assert(validCreditReceipt(receipt(plan(f)), plan(f)));
    f.body.lines.push({ ...line, source_line_id: id(9000) });
    assert.equal(validCreditInput(f.body), false);
    const huge = fixture(),
        capacity = huge.review.capacity.lines[0];
    huge.body.lines[0].net_amount =
        capacity.net_amount =
        capacity.remaining_net_amount =
            "99999999999999999999.98";
    huge.body.lines[0].tax_amount = capacity.tax_amount = capacity.remaining_tax_amount = "0.01";
    capacity.total_amount = capacity.available_gross_amount = "99999999999999999999.99";
    capacity.applied_amount = "0.00";
    const frozen = plan(huge),
        saved = receipt(frozen);
    saved.postings[0].total_amount = capacity.total_amount;
    assert(validCreditReview(ledger, huge.body, huge.review));
    assert(validCreditReceipt(saved, frozen));
    huge.body.lines[0].tax_amount = "0.001";
    assert.equal(validCreditReview(ledger, huge.body, huge.review), false);
});

test("late credit result cannot leak across users, and explicit logout clears the old intent", async () => {
    let finish;
    const f = fixture();
    const controller = new PendingBusinessCreditController(
        (_, frozen) =>
            new Promise((resolve) => {
                finish = () => resolve(receipt(frozen));
            }),
    );
    controller.setOwner(session.user.id);
    const pending = controller.start(session, ledger, f.body, f.review);
    await Promise.resolve();
    controller.setOwner("fictional-other");
    finish();
    await pending;
    assert.equal(controller.getSnapshot().status, "idle");
    controller.setOwner(session.user.id);
    const second = controller.start(session, ledger, f.body, f.review);
    await Promise.resolve();
    controller.setOwner(null, true);
    finish();
    await second;
    controller.setOwner(session.user.id);
    assert.equal(controller.getSnapshot().plan, null);
});

test("malformed credit success remains unknown; explicit initial rejection allows a new intent", async () => {
    const f = fixture();
    for (const code of ["malformed", "period_closed", "idempotency_conflict"]) {
        const controller = new PendingBusinessCreditController(async (_, frozen) => {
            if (code === "malformed") return { ...receipt(frozen), postings: [] };
            throw new ApiError("server", 409, code);
        });
        controller.setOwner(session.user.id);
        await controller.start(session, ledger, f.body, f.review);
        const expected = {
            malformed: "unknown",
            period_closed: "rejected",
            idempotency_conflict: "idempotency-conflict",
        }[code];
        assert.equal(controller.getSnapshot().status, expected);
        assert.equal(controller.dismiss(), code === "period_closed");
    }
});
