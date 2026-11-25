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
const { checkActivity, limit, readActivity } = await import("../src/project-activity.ts");
const q = { ledger: "L1", project: "P1", start: "2026-09-01", end: "2026-09-30", offset: 25 };
const row = {
    asset_id: "Fictional:network:token",
    income: "99999999999999999999.000000000000000001",
    cost: "99999999999999999999.000000000000000002",
    net: "-0.000000000000000001",
    closing_receivable: "0.000000000000000000",
    closing_payable: "-0.000000000000000001",
    attributed_received: "0.000000000000000001",
    attributed_paid: "0.000000000000000000",
};
const page = () => ({
    ledger_id: q.ledger,
    project_id: q.project,
    start: q.start,
    end: q.end,
    offset: q.offset,
    limit,
    has_more: false,
    items: [{ ...row }],
});
test("retain all 38 digits and signed closing balances; reject malformed or mismatched project amounts", () => {
    assert.equal(checkActivity(page(), q), true);
    for (const detail of [
        { net: "0.000000000000000000" },
        { net: -1e-18 },
        { cost: "99999999999999999999.0000000000000000020" },
        { closing_receivable: "-0.000000000000000000" },
        { closing_payable: "0" },
        { income: "1e20" },
        { income: "100000000000000000000" },
        { closing_payable: null },
        { attributed_paid: "-0.000000000000000001" },
        { attributed_received: "0.00" },
    ]) {
        assert.equal(checkActivity({ ...page(), items: [{ ...row, ...detail }] }, q), false);
    }
});
test("no cross scope, date, or page reuse; one row per asset", () => {
    for (const change of [
        { ledger_id: "L2" },
        { project_id: "P2" },
        { start: q.end },
        { end: q.start },
        { offset: 0 },
        { limit: 50 },
        { items: [row, row] },
        { has_more: true },
        { has_more: "false" },
        { items: null },
    ])
        assert.equal(checkActivity({ ...page(), ...change }, q), false);
    const items = Array.from({ length: limit }, (_, i) => ({ ...row, asset_id: "Synthetic" + i }));
    assert.equal(checkActivity({ ...page(), items, has_more: true }, q), true);
    assert.equal(
        checkActivity({ ...page(), items: [...items, { ...row, asset_id: "more" }] }, q),
        false,
    );
});
test("a read carries its exact query and cancellation signal; an invalid reply is not reused", async () => {
    const original = globalThis.fetch;
    const stop = new AbortController();
    let calls = 0;
    globalThis.fetch = async (url, opts) => {
        calls++;
        const u = new URL(url, "https://fictional.invalid");
        assert.equal(u.pathname, "/api/v1/ledgers/L1/business-projects/P1/activity");
        assert.deepEqual(Object.fromEntries(u.searchParams), {
            start: q.start,
            end: q.end,
            offset: "25",
            limit: "25",
        });
        assert.ok(opts.signal instanceof AbortSignal);
        if (calls === 2) {
            assert.equal(opts.signal.aborted, false);
            return new Promise((_resolve, reject) =>
                opts.signal.addEventListener("abort", () => reject(new Error("Aborted")), {
                    once: true,
                }),
            );
        }
        return Response.json({ ...page(), project_id: "P2" });
    };
    try {
        await assert.rejects(readActivity(q, stop.signal), (e) => e.code === "invalid_activity");
        const pending = readActivity(q, stop.signal);
        stop.abort();
        await assert.rejects(pending, (e) => e.kind === "network");
        assert.equal(calls, 2);
    } finally {
        globalThis.fetch = original;
    }
});
