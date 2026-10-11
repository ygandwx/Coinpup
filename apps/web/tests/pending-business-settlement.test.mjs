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
const { PendingBusinessSettlementController } =
    await import("../src/pending-business-settlement.ts");
const { validSettlementInput } = await import("../src/settlement-intent.ts");
const id = (n) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const ledger = id(2),
    session = { user: { id: "fictional-owner" }, csrf_token: "fictional-old" };
const fresh = { ...session, csrf_token: "fictional-fresh" };
function input(kind = "payment", initial = true, count = 2) {
    const group = {
        id: id(4),
        allocation_date: "2026-09-30",
        lines: Array.from({ length: count }, (_, n) => ({
            document_id: id(10),
            document_line_id: id(20 + n),
            payment_amount: "0.01",
            document_amount: "0.01",
        })),
    };
    return kind === "allocation"
        ? { kind, body: { ...group, payment_id: id(1) } }
        : {
              kind,
              body: {
                  id: id(1),
                  direction: "payout",
                  party_id: id(3),
                  account_id: id(5),
                  asset_id: "USD",
                  amount: "110.00",
                  transaction_date: "2026-09-15",
                  description: "Fictional 结算",
                  fees: [
                      {
                          account_id: id(5),
                          asset_id: "BTC",
                          amount: "0.00000001",
                          category_id: id(6),
                      },
                  ],
                  initial_allocation: initial ? group : null,
              },
          };
}
function receipt(plan) {
    const body = JSON.parse(plan.bodyJson);
    const group = (value, payment) => ({
        kind: "business_allocation",
        id: value.id,
        ledger_id: plan.ledgerId,
        payment_id: payment,
        postings: value.lines.map((row, index) => ({
            line_no: index + 1,
            document_id: row.document_id,
            document_line_id: row.document_line_id,
            operation_id: id(1000 + index),
            journal_id: id(2000 + index),
        })),
    });
    return plan.kind === "allocation"
        ? group(body, body.payment_id)
        : {
              kind: "business_payment",
              id: body.id,
              ledger_id: plan.ledgerId,
              operation_id: id(7),
              journal_id: id(8),
              initial_allocation: body.initial_allocation
                  ? group(body.initial_allocation, body.id)
                  : null,
          };
}
function controller(io) {
    const result = new PendingBusinessSettlementController(io);
    result.setOwner(session.user.id);
    return result;
}

for (const kind of ["payment", "allocation"])
    test(`${kind} freezes caller intent and complete receipt`, async () => {
        let returned, observed;
        const command = input(kind);
        const c = controller(async (_, plan) => {
            observed = plan;
            returned = receipt(plan);
            return returned;
        });
        const sent = c.start(session, ledger, command, { note: "Fictional original review" });
        command.body.description = "Fictional later edit";
        await sent;
        assert.equal(c.getSnapshot().status, "confirmed");
        assert.equal(JSON.parse(observed.reviewedJson).note, "Fictional original review");
        assert.notEqual(JSON.parse(observed.bodyJson).description, command.body.description);
        assert(Object.isFrozen(observed));
        const group =
            kind === "payment"
                ? c.getSnapshot().receipt.initial_allocation
                : c.getSnapshot().receipt;
        assert(Object.isFrozen(group.postings) && Object.isFrozen(group.postings[0]));
        (kind === "payment" ? returned.initial_allocation : returned).postings[0].document_id =
            id(999);
        assert.equal(group.postings[0].document_id, id(10));
    });

test("duplicate and re-entrant submit cannot send a second payment", async () => {
    let resolve,
        calls = 0,
        rejected;
    const c = controller(async (_, plan) => {
        calls++;
        return new Promise((done) => {
            resolve = () => done(receipt(plan));
        });
    });
    const unwatch = c.subscribe(() => {
        if (c.getSnapshot().status === "submitting")
            rejected = assert.rejects(
                c.start(session, ledger, input()),
                /pending_settlement_unresolved/u,
            );
    });
    const first = c.start(session, ledger, input());
    await rejected;
    await assert.rejects(c.retry(session));
    assert.equal(c.dismiss(), false);
    assert.equal(calls, 1);
    unwatch();
    resolve();
    await first;
    assert.equal(c.getSnapshot().status, "confirmed");
    assert.equal(c.dismiss(), true);
});

test("unknown retries preserve exact key/body despite capacity and period errors", async () => {
    const attempts = [];
    const errors = [
        new ApiError("network"),
        new ApiError("server", 422, "allocation_exceeds_payment"),
        new ApiError("server", 409, "period_closed"),
    ];
    const c = controller(async (current, plan) => {
        attempts.push({ current, plan });
        if (errors.length) throw errors.shift();
        return receipt(plan);
    });
    await c.start(session, ledger, input());
    const original = c.getSnapshot().plan;
    assert.equal(c.getSnapshot().status, "unknown");
    await assert.rejects(c.start(session, ledger, input("allocation")));
    for (let i = 0; i < 2; i++) {
        await c.retry(fresh);
        assert.equal(c.getSnapshot().status, "unknown");
        assert.equal(c.dismiss(), false);
    }
    await c.retry(fresh);
    assert.equal(c.getSnapshot().status, "confirmed");
    assert(attempts.every(({ plan }) => plan === original));
    assert(attempts.slice(1).every(({ current }) => current.csrf_token === "fictional-fresh"));
});

test("first definite rejection can be dismissed but key conflict stays blocked", async () => {
    const c = controller(async () => {
        throw new ApiError("server", 422, "allocation_date");
    });
    await c.start(session, ledger, input());
    assert.equal(c.getSnapshot().status, "rejected");
    assert(c.dismiss());
    const conflict = controller(async () => {
        throw new ApiError("server", 409, "idempotency_conflict");
    });
    await conflict.start(session, ledger, input());
    assert.equal(conflict.getSnapshot().status, "idempotency-conflict");
    assert.equal(conflict.dismiss(), false);
    await assert.rejects(conflict.retry(session));
});

test("401 hides private state and same-user recovery requires explicit original retry", async () => {
    let calls = 0;
    const c = controller(async (_, plan) => {
        if (++calls === 1) throw new ApiError("unauthorized", 401);
        return receipt(plan);
    });
    await c.start(session, ledger, input());
    const original = c.getSnapshot().plan;
    c.setOwner(null);
    assert.deepEqual(c.getSnapshot(), {
        status: "auth-required",
        plan: null,
        receipt: null,
        error: null,
    });
    c.setOwner(session.user.id);
    assert.equal(c.getSnapshot().status, "unknown");
    assert.equal(calls, 1);
    await c.retry(fresh);
    assert.equal(c.getSnapshot().status, "confirmed");
    assert.equal(c.getSnapshot().plan, original);
});

for (const explicit of [false, true])
    test(`owner reset ignores late responses (${explicit})`, async () => {
        let finish;
        const c = controller(
            async (_, plan) =>
                new Promise((done) => {
                    finish = () => done(receipt(plan));
                }),
        );
        const pending = c.start(session, ledger, input());
        await Promise.resolve();
        c.setOwner(explicit ? null : "fictional-other", explicit);
        finish();
        await pending;
        assert.equal(c.getSnapshot().plan, null);
        assert.equal(c.getSnapshot().receipt, null);
        await assert.rejects(c.retry(session));
    });

test("malformed or incomplete financial receipts always remain unknown", async () => {
    const mutations = [
        (r) => {
            r.ledger_id = id(999);
        },
        (r) => {
            r.id = id(999);
        },
        (r) => {
            r.initial_allocation = null;
        },
        (r) => {
            r.initial_allocation.payment_id = id(999);
        },
        (r) => {
            r.initial_allocation.id = id(999);
        },
        (r) => {
            r.initial_allocation.postings.pop();
        },
        (r) => {
            r.initial_allocation.postings.reverse();
        },
        (r) => {
            r.initial_allocation.postings[1].operation_id =
                r.initial_allocation.postings[0].operation_id;
        },
        (r) => {
            r.initial_allocation.postings[1].journal_id =
                r.initial_allocation.postings[0].journal_id;
        },
        (r) => {
            r.initial_allocation.postings[0].operation_id = r.operation_id;
        },
        (r) => {
            r.initial_allocation.postings[0].document_line_id = id(999);
        },
        (r) => {
            r.initial_allocation.postings[0].line_no = 0;
        },
        (r) => {
            r.fictional_extra = true;
        },
    ];
    for (const mutate of mutations) {
        const c = controller(async (_, plan) => {
            const value = receipt(plan);
            mutate(value);
            return value;
        });
        await c.start(session, ledger, input());
        assert.equal(c.getSnapshot().status, "unknown");
        assert.equal(c.dismiss(), false);
    }
});

test("200 members must be complete and unexpected initial allocations are rejected", async () => {
    const complete = controller(async (_, plan) => receipt(plan));
    await complete.start(session, ledger, input("allocation", true, 200));
    assert.equal(complete.getSnapshot().receipt.postings.length, 200);
    const short = controller(async (_, plan) => {
        const value = receipt(plan);
        value.postings.pop();
        return value;
    });
    await short.start(session, ledger, input("allocation", true, 200));
    assert.equal(short.getSnapshot().status, "unknown");
    const unexpected = controller(async (_, plan) => ({
        ...receipt(plan),
        initial_allocation: {},
    }));
    await unexpected.start(session, ledger, input("payment", false));
    assert.equal(unexpected.getSnapshot().status, "unknown");
});

test("invalid input cannot reach transport or normalize numeric amounts", async () => {
    const mutations = [
        (v) => {
            v.body.amount = 110;
        },
        (v) => {
            v.body.amount = "1e2";
        },
        (v) => {
            v.body.fees = null;
        },
        (v) => {
            v.body.transaction_date = "2026-02-31";
        },
        (v) => {
            v.body.initial_allocation.lines[1].document_line_id =
                v.body.initial_allocation.lines[0].document_line_id;
        },
        (v) => {
            v.body.fees[0].amount = "0";
        },
        (v) => {
            v.body.initial_allocation.lines = [];
        },
    ];
    for (const mutate of mutations) {
        const value = input();
        mutate(value);
        assert.equal(validSettlementInput(ledger, value), false);
        const c = controller(async () => {
            throw new Error("Invalid input reached transport");
        });
        await assert.rejects(c.start(session, ledger, value), /invalid_settlement_intent/u);
        assert.equal(c.getSnapshot().status, "idle");
    }
    assert.equal(validSettlementInput(ledger, input("allocation", true, 201)), false);
});

for (const kind of ["payment", "allocation"])
    test(`${kind} transport preserves path, body, key and fresh CSRF`, async () => {
        const original = globalThis.fetch,
            calls = [];
        try {
            globalThis.fetch = async (url, options) => {
                calls.push({ url, options });
                if (calls.length === 1) throw new Error("Fictional lost response");
                return new Response(
                    JSON.stringify(receipt({ kind, ledgerId: ledger, bodyJson: options.body })),
                    {
                        status: 201,
                        headers: { "Content-Type": "application/json" },
                    },
                );
            };
            const c = controller();
            await c.start(session, ledger, input(kind));
            await c.retry(fresh);
            assert.equal(c.getSnapshot().status, "confirmed");
            assert.equal(calls.length, 2);
            assert.equal(calls[0].url, `/api/v1/ledgers/${ledger}/business-${kind}s`);
            assert.equal(calls[0].options.body, calls[1].options.body);
            assert.equal(
                calls[0].options.headers["Idempotency-Key"],
                calls[1].options.headers["Idempotency-Key"],
            );
            assert.equal(calls[1].options.headers["X-CSRF-Token"], fresh.csrf_token);
        } finally {
            globalThis.fetch = original;
        }
    });
