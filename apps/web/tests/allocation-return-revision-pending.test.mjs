import assert from "node:assert/strict";
import { test } from "node:test";
import { id, setupReturn, response } from "./return-revision-fixture.mjs";
const { PendingBusinessSettlementController } =
    await import("../src/pending-business-settlement.ts");
const { ApiError } = await import("../src/api.ts");
const session = { user: { id: id(77) }, csrf_token: "fictional-session" };
const start = (c, d, cmd) =>
    c.start(session, d.ledger, { kind: "return-revision", revision: cmd }, d.review);
for (const action of ["correct", "cancel"])
    test(
        "return " +
            action +
            " replays original bytes and validates the entire fixed receipt after unknown/401",
        async () => {
            const d = setupReturn(2),
                cmd = d[action],
                saved = response(d, cmd),
                requests = [];
            const source = JSON.stringify(cmd.body);
            const c = new PendingBusinessSettlementController(async (s, p) => {
                requests.push({ session: s, plan: p });
                switch (requests.length) {
                    case 1:
                        throw new ApiError("network");
                    case 2:
                        throw new ApiError("unauthorized", 401, "session_expired");
                    case 3:
                        throw new ApiError("server", 409, "allocation_return_refund_reference");
                    case 4:
                        return { ...saved, members: saved.members.slice(0, -1) };
                    default:
                        return saved;
                }
            });
            c.setOwner(session.user.id);
            await start(c, d, cmd);
            const frozen = c.getSnapshot().plan;
            assert.equal(c.getSnapshot().status, "unknown");
            assert.equal(frozen.kind, "return-revision");
            assert.equal(frozen.action, action);
            assert.equal(frozen.returnId, d.group);
            assert.equal(frozen.ledgerId, d.ledger);
            assert.equal(frozen.bodyJson, source);
            assert.equal(frozen.ownerId, session.user.id);
            assert.ok(Object.isFrozen(frozen));
            d.review.current.members.reverse();
            cmd.body.reason = "Fictional changed after loss";
            assert.equal(c.dismiss(), false);
            await assert.rejects(
                start(c, d, cmd),
                (e) => e.code === "pending_settlement_unresolved",
            );
            await c.retry(session);
            assert.equal(c.getSnapshot().status, "auth-required");
            c.setOwner(null);
            assert.equal(c.getSnapshot().plan, null);
            assert.equal(c.getSnapshot().receipt, null);
            c.setOwner(session.user.id);
            assert.equal(c.getSnapshot().status, "unknown");
            for (const attempt of [3, 4]) {
                await c.retry(session);
                assert.equal(c.getSnapshot().status, "unknown");
                assert.equal(requests.length, attempt);
            }
            await c.retry({ ...session, csrf_token: "fictional-renewed" });
            assert.equal(c.getSnapshot().status, "confirmed");
            assert.ok(requests.every((r) => r.plan === frozen));
            assert.ok(Object.isFrozen(c.getSnapshot().receipt.members[0]));
            assert.deepEqual(c.getSnapshot().receipt, saved);
            assert.equal(c.dismiss(), true);
        },
    );
test("default write transport sends the scoped route; late results after owner change cannot disclose a prior return", async (t) => {
    const d = setupReturn(),
        started = [];
    const mock = t.mock.method(globalThis, "fetch", async (url, init) => {
        const s = response(d, d.cancel);
        started.push({ url, init });
        return new Response(
            JSON.stringify(s).replace(/"source_return_no":"(\d+)"/gu, '"source_return_no":$1'),
            { status: 200, headers: { "content-type": "application/json" } },
        );
    });
    const c = new PendingBusinessSettlementController();
    c.setOwner(session.user.id);
    await start(c, d, d.cancel);
    assert.equal(c.getSnapshot().status, "confirmed");
    assert.equal(c.getSnapshot().receipt.members[0].source_return_no, "9007199254740993");
    assert.equal(
        started[0].url,
        "/api/v1/ledgers/" +
            d.ledger +
            "/business-allocation-returns/" +
            d.group +
            "/cancellations",
    );
    assert.equal(started[0].init.body, JSON.stringify(d.cancel.body));
    assert.equal(
        new Headers(started[0].init.headers).get("Idempotency-Key"),
        c.getSnapshot().plan.key,
    );
    mock.mock.restore();
    let finish;
    const delayed = new PendingBusinessSettlementController(
        () =>
            new Promise((resolve) => {
                finish = resolve;
            }),
    );
    delayed.setOwner(session.user.id);
    const flight = start(delayed, d, d.correct);
    await Promise.resolve();
    delayed.setOwner(null);
    delayed.setOwner(id(88));
    finish(response(d, d.correct));
    await flight;
    assert.equal(delayed.getSnapshot().status, "idle");
    assert.equal(delayed.getSnapshot().receipt, null);
});
test("definite refusal before any uncertainty is editable and malformed reviews issue zero requests", async () => {
    const d = setupReturn();
    let calls = 0;
    const c = new PendingBusinessSettlementController(async () => {
        calls++;
        throw new ApiError("server", 409, "allocation_return_refund_reference");
    });
    c.setOwner(session.user.id);
    await start(c, d, d.cancel);
    assert.equal(c.getSnapshot().status, "rejected");
    assert.equal(c.dismiss(), true);
    d.correct.body.replacement.lines[0].document_amount = "0.00000010";
    await assert.rejects(start(c, d, d.correct), (e) => e.code === "invalid_settlement_intent");
    assert.equal(calls, 1);
});
