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
const { PendingBusinessSettlementController } =
    await import("../src/pending-business-settlement.ts");
const { ApiError } = await import("../src/api.ts");
const { refundRevision, id } = await import("./refund-revision-fixture.mjs");
const session = { user: { id: id(30) }, csrf_token: "fictional-first" };
const start = (controller, f) =>
    controller.start(
        session,
        f.scope.ledger_id,
        { kind: "refund-revision", revision: f.input },
        f.review,
    );
const ready = (transport) => {
    const controller = new PendingBusinessSettlementController(transport);
    controller.setOwner(session.user.id);
    return controller;
};
for (const action of ["correct", "cancel"]) {
    test(
        action +
            " freezes the path, ordered raw JSON, owner, ledger and review across uncertain outcomes",
        async () => {
            const f = refundRevision(action),
                sent = [];
            const saved = structuredClone(f.result);
            const c = ready(async (s, p) => {
                sent.push({ session: s, plan: p });
                if (sent.length === 1) throw new ApiError("network");
                if (sent.length === 2) throw new ApiError("unauthorized", 401, "session_expired");
                if (sent.length === 3) throw new ApiError("server", 409, "period_closed");
                return saved;
            });
            await start(c, f);
            assert.equal(c.getSnapshot().status, "unknown");
            const expected = c.getSnapshot().plan;
            assert.equal(expected.kind, "refund-revision");
            assert.equal(expected.action, action);
            assert.equal(expected.refundId, f.input.refundId);
            assert.equal(expected.ownerId, session.user.id);
            assert.equal(expected.ledgerId, f.scope.ledger_id);
            assert(Object.isFrozen(expected));
            assert.equal(c.dismiss(), false);
            f.input.body.reason = "Fictional changed editor";
            f.review.current.version = 40;
            await assert.rejects(start(c, f), (e) => e.code === "pending_settlement_unresolved");
            await c.retry(session);
            assert.equal(c.getSnapshot().status, "auth-required");
            c.setOwner(null);
            assert.equal(c.getSnapshot().plan, null);
            c.setOwner(session.user.id);
            assert.equal(c.getSnapshot().status, "unknown");
            const fresh = { ...session, csrf_token: "fictional-fresh" };
            await c.retry(fresh);
            assert.equal(c.getSnapshot().status, "unknown");
            await c.retry(fresh);
            assert.equal(c.getSnapshot().status, "confirmed");
            assert.deepEqual(c.getSnapshot().receipt, saved);
            assert(Object.isFrozen(c.getSnapshot().receipt.previous.fees));
            assert(sent.every((x) => x.plan === expected));
            assert.equal(sent.at(-1).session.csrf_token, "fictional-fresh");
            assert(!JSON.stringify(expected).includes("fictional-first"));
        },
    );
}
test("refund revisions serialize against existing settlement submissions and ignore a different owner's late success", async () => {
    const f = refundRevision(),
        other = id(45);
    let done,
        calls = 0;
    const c = ready(() => {
        calls++;
        return new Promise((res) => (done = res));
    });
    const first = start(c, f);
    await Promise.resolve();
    await assert.rejects(start(c, f), (e) => e.code === "pending_settlement_unresolved");
    await assert.rejects(
        c.start(session, f.scope.ledger_id, { kind: "refund", body: {} }),
        (e) => e.code === "pending_settlement_unresolved",
    );
    assert.equal(calls, 1);
    c.setOwner(other);
    done(f.result);
    await first;
    assert.equal(c.getSnapshot().status, "idle");
    assert.equal(c.getSnapshot().receipt, null);
});
test("malformed success stays unknown while a definite rejection allows a fresh explicit intention", async () => {
    const f = refundRevision();
    const c = ready(async () => ({ ...f.result, previous_version: 2 }));
    await start(c, f);
    assert.equal(c.getSnapshot().status, "unknown");
    const denied = ready(async () => {
        throw new ApiError("server", 409, "period_closed");
    });
    await start(denied, f);
    assert.equal(denied.getSnapshot().status, "rejected");
    assert(denied.dismiss());
    const conflict = ready(async () => {
        throw new ApiError("server", 409, "idempotency_conflict");
    });
    await start(conflict, f);
    assert.equal(conflict.getSnapshot().status, "idempotency-conflict");
    assert(!conflict.dismiss());
    await assert.rejects(conflict.retry(session));
});
test("invalid or stale intent never sends a request or allocates an active plan", async () => {
    const f = refundRevision();
    let calls = 0;
    const c = ready(() => {
        calls++;
    });
    f.input.body.expected_version = 2;
    await assert.rejects(start(c, f), (e) => e.code === "invalid_settlement_intent");
    assert.equal(calls, 0);
    assert.equal(c.getSnapshot().plan, null);
});
test("default transport uses the fresh session and the stable refund path for both commands", async (t) => {
    for (const action of ["correct", "cancel"]) {
        const f = refundRevision(action);
        const calls = [];
        t.mock.method(globalThis, "fetch", async (path, init) => {
            calls.push([path, init]);
            return new Response(JSON.stringify(f.result), {
                status: 200,
                headers: { "Content-Type": "application/json" },
            });
        });
        const c = ready(undefined);
        await start(c, f);
        assert.equal(c.getSnapshot().status, "confirmed");
        assert.equal(calls.length, 1);
        const [path, init] = calls[0];
        assert.equal(
            path,
            `/api/v1/ledgers/${f.scope.ledger_id}/business-refunds/${f.input.refundId}/${action === "correct" ? "corrections" : "cancellations"}`,
        );
        const headers = new Headers(init.headers);
        assert.equal(headers.get("Idempotency-Key"), c.getSnapshot().plan.key);
        assert.equal(init.body, c.getSnapshot().plan.bodyJson);
        t.mock.restoreAll();
    }
});
