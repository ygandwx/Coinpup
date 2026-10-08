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
const { PendingPeriodController } = await import("../src/pending-period.ts");
const api = await import("../src/periods-api.ts");
const session = { user: { id: "fictional-owner" }, csrf_token: "fictional-old" };
const fresh = { ...session, csrf_token: "fictional-fresh" };
const other = { user: { id: "fictional-other" }, csrf_token: "fictional-other" };
const input = () => ({
    action: "close",
    closed_through: "2026-01-31",
    expected_version: 1,
    reason: " Fictional close ",
});
function receipt(plan) {
    const body = JSON.parse(plan.bodyJson);
    return {
        id: "fictional-audit",
        actor_id: plan.ownerId,
        ledger_id: plan.ledgerId,
        action: body.action,
        reason: body.reason.trim(),
        version: body.expected_version + 1,
        closed_through: body.closed_through,
        previous_closed_through: null,
        created_at: "2026-01-31T12:00:00Z",
    };
}
function controller(io) {
    const value = new PendingPeriodController(io);
    value.setOwner(session.user.id);
    return value;
}
test("lost response and 401 retain exact intent until same-owner manual retry", async () => {
    const calls = [];
    const value = controller(async (s, plan) => {
        calls.push({ ...plan, csrf: s.csrf_token });
        if (calls.length === 1) throw new ApiError("network");
        if (calls.length === 2) throw new ApiError("unauthorized", 401);
        return receipt(plan);
    });
    const body = input();
    const first = value.start(session, "fictional-ledger", body);
    body.reason = "Changed after freezing";
    await assert.rejects(value.start(session, "fictional-other-ledger", input()));
    await first;
    assert.equal(value.getSnapshot().status, "unknown");
    assert.equal(value.dismiss(), false);
    await value.retry(session);
    value.setOwner(null);
    assert.equal(value.getSnapshot().plan, null);
    value.setOwner(session.user.id);
    assert.equal(value.getSnapshot().status, "unknown");
    assert.equal(calls.length, 2);
    await value.retry(fresh);
    assert.equal(value.getSnapshot().status, "confirmed");
    assert.equal(new Set(calls.map((c) => c.key)).size, 1);
    assert.equal(new Set(calls.map((c) => c.bodyJson)).size, 1);
    assert.equal(JSON.parse(calls[2].bodyJson).reason, input().reason);
    assert.equal(calls[2].csrf, fresh.csrf_token);
});
test("owner switch and explicit logout discard the intent and ignore late responses", async () => {
    for (const logout of [false, true]) {
        let finish;
        const value = controller(
            (s, plan) =>
                new Promise((resolve) => {
                    finish = () => resolve(receipt(plan));
                }),
        );
        const request = value.start(session, "fictional-ledger", input());
        await Promise.resolve();
        value.setOwner(logout ? null : other.user.id, logout);
        finish();
        await request;
        assert.equal(value.getSnapshot().status, "idle");
        assert.equal(value.getSnapshot().receipt, null);
        await assert.rejects(value.retry(session));
    }
});
test("late receipts stay hidden while signed out and reappear only for the same owner", async () => {
    let finish;
    const value = controller(
        (s, plan) =>
            new Promise((resolve) => {
                finish = () => resolve(receipt(plan));
            }),
    );
    const request = value.start(session, "fictional-ledger", input());
    await Promise.resolve();
    value.setOwner(null);
    finish();
    await request;
    assert.equal(value.getSnapshot().receipt, null);
    value.setOwner(session.user.id);
    assert.equal(value.getSnapshot().status, "confirmed");
});
test("receipt mismatches and key conflicts never authorize a new key", async () => {
    for (const change of [
        { ledger_id: "other" },
        { actor_id: "other" },
        { version: 8 },
        { action: "reopen" },
        { closed_through: null },
        { reason: "other" },
        { previous_closed_through: "2026-02-30" },
        { created_at: "invalid" },
    ]) {
        const value = controller(async (s, plan) => ({ ...receipt(plan), ...change }));
        await value.start(session, "fictional-ledger", input());
        assert.equal(value.getSnapshot().status, "unknown");
        assert.equal(value.dismiss(), false);
    }
    const value = controller(async () => {
        throw new ApiError("server", 409, "idempotency_conflict");
    });
    await value.start(session, "fictional-ledger", input());
    assert.equal(value.getSnapshot().status, "idempotency-conflict");
    assert.equal(value.dismiss(), false);
});
test("monotonic version conflict fences an earlier uncertain request", async () => {
    let count = 0;
    const value = controller(async () => {
        if (++count === 1) throw new ApiError("network");
        throw new ApiError("server", 409, "version_conflict");
    });
    await value.start(session, "fictional-ledger", input());
    await value.retry(session);
    assert.equal(value.getSnapshot().status, "rejected");
    assert.equal(value.dismiss(), true);
});
test("invalid calendar/action/version/reason is rejected without transport", async () => {
    const value = controller(async () => assert.fail("Invalid intent reached transport"));
    for (const body of [
        { action: "adjust" },
        { expected_version: 1.5 },
        { expected_version: 0 },
        { expected_version: 2147483647 },
        { reason: " " },
        { reason: "a\0b" },
        { closed_through: null },
        { closed_through: undefined },
        { closed_through: "2026-02-30" },
    ]) {
        await assert.rejects(value.start(session, "fictional-ledger", { ...input(), ...body }));
        assert.equal(value.getSnapshot().status, "idle");
    }
});
test("period transport preserves CSRF, key, body and encoded ledger while reads are abortable", async () => {
    const original = globalThis.fetch,
        calls = [];
    globalThis.fetch = async (url, options) => {
        calls.push({ url, options });
        return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
    };
    try {
        const signal = new AbortController().signal;
        await api.getPeriod("fictional/a", signal);
        await api.periodHistory("fictional/a", 25, signal);
        await api.changePeriod("fictional-csrf", "fictional/a", input(), "fictional-key");
        assert.equal(calls[0].url, "/api/v1/ledgers/fictional%2Fa/period");
        assert.equal(
            calls[1].url,
            "/api/v1/ledgers/fictional%2Fa/period-changes?limit=25&offset=25",
        );
        assert.equal(calls[0].options.signal.aborted, false);
        const cancelled = new AbortController();
        globalThis.fetch = async (_url, options) => {
            cancelled.abort();
            assert.equal(options.signal.aborted, true);
            throw new Error("fictional cancelled request");
        };
        await assert.rejects(api.getPeriod("fictional/a", cancelled.signal));
        assert.equal(calls[2].options.body, JSON.stringify(input()));
        assert.equal(calls[2].options.headers["Idempotency-Key"], "fictional-key");
        assert.equal(calls[2].options.headers["X-CSRF-Token"], "fictional-csrf");
    } finally {
        globalThis.fetch = original;
    }
});
