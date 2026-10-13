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
const { ApiError } = await import("../src/api.ts");
const { PendingConfirmationController } = await import("../src/pending-confirmations.ts");
const id = (n) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const session = {
    user: { id: "fictional-owner", username: "fictional" },
    csrf_token: "fictional-old",
};
const fresh = { ...session, csrf_token: "fictional-fresh" };
const other = {
    user: { id: "fictional-other", username: "fictional-other" },
    csrf_token: "fictional-other",
};
function input(count = 200, draftId = id(7)) {
    return {
        ledgerId: id(8),
        draftId,
        body: {
            expected_version: 3,
            target_ledger_id: id(2),
            target_file_id: id(9),
            confirmed: ["row.1.date", "row.1.amount", "row.1.currency"],
            entry: { kind: "link_payment", payment_id: id(1), expected_version: 4 },
        },
        originalPayment: {
            kind: "business_payment",
            id: id(1),
            ledger_id: id(2),
            operation_id: id(3),
            journal_id: id(4),
            initial_allocation:
                count === 0
                    ? null
                    : {
                          kind: "business_allocation",
                          id: id(5),
                          ledger_id: id(2),
                          payment_id: id(1),
                          postings: Array.from({ length: count }, (_, i) => ({
                              line_no: i + 1,
                              document_id: id(6),
                              document_line_id: id(20 + i),
                              operation_id: id(1000 + i),
                              journal_id: id(2000 + i),
                          })),
                      },
        },
    };
}
function receipt(plan) {
    const body = JSON.parse(plan.bodyJson);
    return {
        intent_id: body.intent_id,
        draft_id: plan.draftId,
        ledger_id: plan.ledgerId,
        draft_version: body.expected_version + 1,
        action: "link_payment",
        target_ledger_id: body.target_ledger_id,
        target_file_id: body.target_file_id,
        operation_version: body.entry.expected_version,
        payment: JSON.parse(plan.originalPaymentJson),
    };
}
function controller(io) {
    const state = new PendingConfirmationController(io);
    state.setOwner(session.user.id);
    return state;
}

test("null/one/200-member original payment receipts are frozen outside the unchanged request", async () => {
    for (const count of [0, 1, 200]) {
        const form = input(count);
        const state = controller(async (_session, plan) => {
            assert.equal(Object.isFrozen(plan), true);
            const body = JSON.parse(plan.bodyJson);
            assert.deepEqual(body.entry, form.body.entry);
            assert.deepEqual(
                Object.keys(body).sort(),
                [...Object.keys(form.body), "intent_id"].sort(),
            );
            assert.notEqual(body.intent_id, form.originalPayment.id);
            return receipt(plan);
        });
        await state.start(session, [form]);
        assert.equal(state.getSnapshot().status, "confirmed");
        assert.deepEqual(state.getSnapshot().receipts[0].payment, form.originalPayment);
    }
});

test("missing, foreign or malformed review fails before any request or pending intent", async () => {
    let calls = 0;
    for (const damage of [
        (p) => {
            delete p.originalPayment;
        },
        (p) => {
            p.originalPayment.ledger_id = id(99);
        },
        (p) => {
            p.originalPayment.id = id(99);
        },
        (p) => {
            p.originalPayment.initial_allocation.postings[199].line_no = 199;
        },
        (p) => {
            p.body.entry.expected_version = 1.5;
        },
        (p) => {
            p.body.entry.expected_version = 0;
        },
    ]) {
        const state = controller(async () => {
            calls++;
        });
        const form = input();
        damage(form);
        await assert.rejects(
            state.start(session, [form]),
            (e) => e.code === "payment_link_review_required",
        );
        assert.equal(state.getSnapshot().status, "idle");
    }
    assert.equal(calls, 0);
});

test("loss and manual retry retain full original payment, raw intent, versions and new CSRF", async () => {
    const calls = [],
        form = input();
    const state = controller(async (user, plan) => {
        calls.push([user.csrf_token, plan]);
        if (calls.length === 1) throw new ApiError("network");
        return receipt(plan);
    });
    await state.start(session, [form]);
    form.body.entry.payment_id = id(99);
    form.body.entry.expected_version = 20;
    form.body.confirmed.reverse();
    form.body.target_ledger_id = id(99);
    form.originalPayment.initial_allocation.postings.pop();
    form.originalPayment.operation_id = id(99);
    assert.equal(state.dismiss(), false);
    await assert.rejects(state.start(session, [form]));
    await state.retry(fresh);
    assert.strictEqual(calls[0][1], calls[1][1]);
    assert.equal(calls[1][0], fresh.csrf_token);
    assert.equal(state.getSnapshot().status, "confirmed");
    assert.equal(state.getSnapshot().receipts[0].payment.initial_allocation.postings.length, 200);
    assert.equal(state.getSnapshot().receipts[0].payment.operation_id, id(3));
});

for (const [name, damage] of [
    [
        "partial 199",
        (r) => {
            r.payment.initial_allocation.postings.pop();
        },
    ],
    [
        "last operation",
        (r) => {
            r.payment.initial_allocation.postings[199].operation_id = id(9999);
        },
    ],
    [
        "last journal",
        (r) => {
            r.payment.initial_allocation.postings[199].journal_id = id(9999);
        },
    ],
    [
        "last source",
        (r) => {
            r.payment.initial_allocation.postings[199].document_line_id = id(9999);
        },
    ],
    [
        "last document",
        (r) => {
            r.payment.initial_allocation.postings[199].document_id = id(9999);
        },
    ],
    [
        "reordered",
        (r) => {
            r.payment.initial_allocation.postings.reverse();
        },
    ],
    [
        "group",
        (r) => {
            r.payment.initial_allocation.id = id(9999);
        },
    ],
    [
        "payment",
        (r) => {
            r.payment.id = id(9999);
        },
    ],
    [
        "version",
        (r) => {
            r.operation_version++;
        },
    ],
    [
        "intent",
        (r) => {
            r.intent_id = id(9999);
        },
    ],
    [
        "draft",
        (r) => {
            r.draft_id = id(9999);
        },
    ],
    [
        "draft version",
        (r) => {
            r.draft_version++;
        },
    ],
    [
        "source ledger",
        (r) => {
            r.ledger_id = id(9999);
        },
    ],
    [
        "target ledger",
        (r) => {
            r.target_ledger_id = id(9999);
        },
    ],
    [
        "target file",
        (r) => {
            r.target_file_id = id(9999);
        },
    ],
    [
        "legacy action",
        (r) => {
            r.action = "link";
        },
    ],
    [
        "extra legacy operation",
        (r) => {
            r.operation = {};
        },
    ],
])
    test(`invalid ${name} cannot advance a batch or prove success`, async () => {
        const calls = [];
        let damaged = true;
        const state = controller(async (_session, plan) => {
            calls.push(plan);
            const value = receipt(plan);
            if (damaged) damage(value);
            return value;
        });
        await state.start(session, [input(), input(0, id(10))]);
        assert.equal(state.getSnapshot().status, "unknown");
        assert.equal(state.getSnapshot().index, 0);
        assert.deepEqual(state.getSnapshot().receipts, []);
        assert.equal(calls.length, 1);
        damaged = false;
        await state.retry(session);
        assert.strictEqual(calls[0], calls[1]);
        assert.equal(state.getSnapshot().index, 2);
        assert.equal(state.getSnapshot().status, "confirmed");
    });

test("real 401 kind redacts the frozen proof and same-owner login never auto-retries", async () => {
    let calls = 0;
    const state = controller(async (_user, plan) => {
        if (++calls === 1) throw new ApiError("unauthorized", 401);
        return receipt(plan);
    });
    await state.start(session, [input()]);
    const plan = state.getSnapshot().plans[0];
    state.setOwner(null);
    assert.deepEqual(state.getSnapshot().plans, []);
    assert.deepEqual(state.getSnapshot().receipts, []);
    await assert.rejects(state.retry(session));
    state.setOwner(session.user.id);
    assert.equal(calls, 1);
    assert.strictEqual(state.getSnapshot().plans[0], plan);
    await state.retry(fresh);
    assert.equal(state.getSnapshot().status, "confirmed");
});

test("logout and owner switch discard a late private receipt and all original proof", async () => {
    for (const logout of [false, true]) {
        let finish;
        const state = controller(
            async (_session, plan) =>
                new Promise((resolve) => {
                    finish = () => resolve(receipt(plan));
                }),
        );
        const flight = state.start(session, [input()]);
        await Promise.resolve();
        state.setOwner(null, logout);
        state.setOwner(other.user.id);
        finish();
        await flight;
        assert.equal(state.getSnapshot().status, "idle");
        assert.deepEqual(state.getSnapshot().plans, []);
        assert.deepEqual(state.getSnapshot().receipts, []);
        await assert.rejects(state.retry(fresh));
    }
});

test("same-flight payment retry shares one dispatch and conflicts retain the original proof", async () => {
    let finish,
        calls = 0;
    const state = controller(async (_session, plan) => {
        if (++calls === 1) throw new ApiError("network");
        return new Promise((resolve) => {
            finish = () => resolve(receipt(plan));
        });
    });
    await state.start(session, [input()]);
    const first = state.retry(fresh),
        second = state.retry(fresh);
    assert.strictEqual(first, second);
    await Promise.resolve();
    finish();
    await first;
    assert.equal(calls, 2);
    for (const code of ["entity_archived", "idempotency_conflict", "version_conflict"]) {
        let attempts = 0;
        const pending = controller(async () => {
            if (++attempts === 1) throw new ApiError("network");
            throw new ApiError("server", 409, code);
        });
        await pending.start(session, [input()]);
        const original = pending.getSnapshot().plans[0];
        await pending.retry(fresh);
        assert.strictEqual(pending.getSnapshot().plans[0], original);
        assert.equal(
            pending.getSnapshot().status,
            code === "version_conflict"
                ? "rejected"
                : code === "idempotency_conflict"
                  ? "idempotency-conflict"
                  : "unknown",
        );
    }
});
