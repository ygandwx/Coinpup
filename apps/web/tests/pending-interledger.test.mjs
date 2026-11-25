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
const { PendingInterledger } = await import("../src/pending-interledger.ts");
const session = { user: { id: "fictional-owner" }, csrf_token: "test-old" };
const uuid = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const body = () => ({
    payer_ledger_id: uuid(1),
    beneficiary_ledger_id: uuid(2),
    payer_account_id: uuid(3),
    expense_category_id: uuid(4),
    asset_id: "USD",
    amount: "100.0",
    transaction_date: "2026-11-12",
    description: "Fictional",
});
const reply = (plan) => ({
    ...JSON.parse(plan.bodyJson),
    id: uuid(5),
    amount: "100.00",
    outstanding_amount: "100.00",
    payer_operation_id: uuid(6),
    beneficiary_operation_id: uuid(7),
});
const fresh = { ...session, csrf_token: "test-new" };
const start = (controller, input = body()) =>
    controller.start(session, uuid(1), 2, { kind: "advance", body: input });

test("same-owner resumption preserves both ledgers, raw amount and original key", async () => {
    const calls = [];
    const controller = new PendingInterledger(async (s, plan) => {
        calls.push({ ...plan, csrf: s.csrf_token });
        if (calls.length === 1) throw new ApiError("network");
        if (calls.length === 2) throw new ApiError("unauthorized", 401);
        return reply(plan);
    });
    controller.setOwner(session.user.id);
    const input = body();
    const first = start(controller, input);
    input.amount = "500.00";
    input.beneficiary_ledger_id = uuid(8);
    await assert.rejects(start(controller));
    await first;
    assert.equal(controller.getSnapshot().status, "unknown");
    assert.equal(controller.dismiss(), false);
    await controller.retry(session);
    assert.equal(controller.getSnapshot().status, "auth-required");
    assert.equal(controller.getSnapshot().plan, null);
    controller.setOwner(fresh.user.id);
    assert.equal(controller.getSnapshot().status, "unknown");
    await controller.retry(fresh);
    assert.equal(controller.getSnapshot().status, "confirmed");
    assert.equal(new Set(calls.map(({ bodyJson }) => bodyJson)).size, 1);
    assert.equal(new Set(calls.map(({ key }) => key)).size, 1);
    assert.deepEqual(JSON.parse(calls[2].bodyJson), body());
    assert.equal(calls[2].csrf, "test-new");
});

test("a malformed member receipt or a response after owner change cannot confirm", async () => {
    const controller = new PendingInterledger(async (_s, plan) => ({
        ...reply(plan),
        beneficiary_operation_id: uuid(6),
    }));
    controller.setOwner(session.user.id);
    await start(controller);
    assert.equal(controller.getSnapshot().status, "unknown");
    assert.equal(controller.getSnapshot().record, null);
    let finish;
    const delayed = new PendingInterledger(
        (_s, plan) =>
            new Promise((resolve) => {
                finish = () => resolve(reply(plan));
            }),
    );
    delayed.setOwner(session.user.id);
    const pending = start(delayed);
    await Promise.resolve();
    delayed.setOwner("fictional-second-owner");
    finish();
    await pending;
    assert.equal(delayed.getSnapshot().status, "idle");
    assert.equal(delayed.getSnapshot().record, null);
});
