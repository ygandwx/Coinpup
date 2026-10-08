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
const api = await import("../src/ocr-api.ts");
const { OcrJobIntents } = await import("../src/ocr-job-intents.ts");
const session = {
    user: { id: "fictional-owner", username: "fictional" },
    csrf_token: "fictional-old",
};
const fresh = { ...session, csrf_token: "fictional-fresh" };
const other = {
    user: { id: "fictional-other", username: "fictional-other" },
    csrf_token: "fictional-other",
};
function input(draftId = "fictional-draft") {
    return {
        ledgerId: "fictional-source",
        draftId,
        body: {
            expected_version: 3,
            target_ledger_id: "fictional-target",
            target_file_id: "fictional-file",
            confirmed: ["header.total", "header.currency"],
            entry: {
                kind: "expense",
                command: {
                    account_id: "fictional-bank",
                    asset_id: "USD",
                    amount: "1.00",
                    transaction_date: "2031-07-18",
                    recognition_date: "2031-07-18",
                    splits: [{ category_id: "fictional-category", amount: "1.00" }],
                },
            },
        },
    };
}
function receipt(plan) {
    const body = JSON.parse(plan.bodyJson);
    const linking = body.entry.kind === "link";
    return {
        intent_id: body.intent_id,
        draft_id: plan.draftId,
        ledger_id: plan.ledgerId,
        draft_version: body.expected_version + 1,
        action: linking ? "link" : "create",
        target_ledger_id: body.target_ledger_id,
        target_file_id: body.target_file_id,
        operation: {
            ...input().body.entry.command,
            ...(linking ? {} : body.entry.command),
            id: linking ? body.entry.operation_id : body.entry.command.id,
            ledger_id: body.target_ledger_id,
            kind: linking ? "expense" : body.entry.kind,
            version: linking ? body.entry.expected_version : 1,
            journal_id: "fictional-journal",
            description: "",
            created_at: "2031-07-18T00:00:00Z",
        },
    };
}
function controller(io) {
    const value = new PendingConfirmationController(io);
    value.setOwner(session.user.id);
    return value;
}

test("job start retains unknown intent through login and clears it when the owner changes", async () => {
    const original = globalThis.fetch,
        calls = [],
        state = new OcrJobIntents();
    globalThis.fetch = async (_path, init) => {
        calls.push(JSON.parse(init.body));
        if (calls.length === 1) throw new TypeError("fictional lost response");
        return new Response(JSON.stringify({ ...calls.at(-1), ledger_id: "ledger" }), {
            status: 201,
            headers: { "content-type": "application/json" },
        });
    };
    try {
        state.setOwner(session.user.id);
        await assert.rejects(state.start(session, "ledger", "file"));
        state.setOwner(null);
        await assert.rejects(state.start(session, "ledger", "file"));
        state.setOwner(session.user.id);
        await state.start(fresh, "ledger", "file");
        assert.deepEqual(calls[0], calls[1]);
        state.setOwner(other.user.id);
        await state.start(other, "ledger", "file");
        assert.notEqual(calls[1].intent_id, calls[2].intent_id);
    } finally {
        globalThis.fetch = original;
    }
});

test("unknown confirmation retries the frozen body and fresh CSRF, not edited form or ledger", async () => {
    const calls = [];
    const state = controller(async (user, plan) => {
        calls.push([user.csrf_token, plan]);
        if (calls.length === 1) throw new ApiError("network");
        return receipt(plan);
    });
    const form = input();
    await state.start(session, [form]);
    form.ledgerId = "changed-ledger";
    form.body.entry.command.amount = "999.00";
    form.body.confirmed.reverse();
    assert.equal(state.getSnapshot().status, "unknown");
    assert.equal(state.dismiss(), false);
    await assert.rejects(state.start(session, [form]));
    await state.retry(fresh);
    assert.equal(calls[0][1].bodyJson, calls[1][1].bodyJson);
    assert.equal(calls[1][0], "fictional-fresh");
    assert.equal(JSON.parse(calls[1][1].bodyJson).entry.command.amount, "1.00");
    assert.equal(state.getSnapshot().status, "confirmed");
});

test("batch keeps successes, stops at an unknown row and never dispatches later rows early", async () => {
    const calls = [];
    let fail = true;
    const state = controller(async (_session, plan) => {
        calls.push(plan);
        if (plan.draftId === "second" && fail) {
            fail = false;
            throw new ApiError("network");
        }
        return receipt(plan);
    });
    await state.start(session, [input("first"), input("second"), input("third")]);
    assert.deepEqual(
        calls.map((p) => p.draftId),
        ["first", "second"],
    );
    assert.equal(state.getSnapshot().receipts.length, 1);
    assert.equal(state.getSnapshot().index, 1);
    await state.retry(session);
    assert.deepEqual(
        calls.map((p) => p.draftId),
        ["first", "second", "second", "third"],
    );
    assert.equal(calls[1].bodyJson, calls[2].bodyJson);
    assert.equal(state.getSnapshot().receipts.length, 3);
    assert.equal(state.getSnapshot().status, "confirmed");
});

test("401 redacts pending data; same-owner login enables only manual original-intent retry", async () => {
    let calls = 0;
    const state = controller(async (_session, plan) => {
        if (++calls === 1) throw new ApiError("unauthorized", 401);
        return receipt(plan);
    });
    await state.start(session, [input()]);
    const frozen = state.getSnapshot().plans[0].bodyJson;
    state.setOwner(null);
    assert.deepEqual(state.getSnapshot().plans, []);
    assert.deepEqual(state.getSnapshot().receipts, []);
    await assert.rejects(state.retry(session));
    state.setOwner(session.user.id);
    assert.equal(calls, 1);
    assert.equal(state.getSnapshot().plans[0].bodyJson, frozen);
    await state.retry(fresh);
    assert.equal(state.getSnapshot().status, "confirmed");
});

test("owner expiry during a flight prevents the next row, and another owner cannot see late results", async () => {
    let resolve;
    let calls = 0;
    const state = controller(async (_session, plan) => {
        calls++;
        return new Promise((done) => {
            resolve = () => done(receipt(plan));
        });
    });
    const flight = state.start(session, [input("first"), input("second")]);
    await Promise.resolve();
    state.setOwner(null);
    resolve();
    await flight;
    assert.equal(calls, 1);
    assert.deepEqual(state.getSnapshot().receipts, []);
    state.setOwner(other.user.id);
    assert.equal(state.getSnapshot().status, "idle");
    assert.deepEqual(state.getSnapshot().plans, []);
    await assert.rejects(state.retry(session));
});

test("explicit logout discards late responses and cannot overwrite a new user's request", async () => {
    let resolve;
    const state = controller(
        async (_session, plan) =>
            new Promise((done) => {
                resolve = () => done(receipt(plan));
            }),
    );
    const flight = state.start(session, [input()]);
    await Promise.resolve();
    state.setOwner(null, true);
    state.setOwner(other.user.id);
    resolve();
    await flight;
    assert.equal(state.getSnapshot().status, "idle");
    assert.deepEqual(state.getSnapshot().receipts, []);
});

for (const damage of ["identity", "money", "version", "html"])
    test(`malformed ${damage} receipt is unknown`, async () => {
        const state = controller(async (_session, plan) => {
            const value = receipt(plan);
            if (damage === "identity") value.target_file_id = "wrong";
            if (damage === "money") value.operation.amount = 1;
            if (damage === "version") value.operation.version = 2;
            return damage === "html" ? "<html>Fictional login page</html>" : value;
        });
        await state.start(session, [input()]);
        assert.equal(state.getSnapshot().status, "unknown");
        assert.equal(state.dismiss(), false);
    });

test("ordinary rejection after an uncertain attempt stays frozen; monotonic version rejection resolves it", async () => {
    let attempt = 0;
    const state = controller(async () => {
        attempt++;
        if (attempt === 1) throw new ApiError("network");
        throw new ApiError("server", 409, attempt === 2 ? "entity_archived" : "version_conflict");
    });
    await state.start(session, [input()]);
    await state.retry(session);
    assert.equal(state.getSnapshot().status, "unknown");
    await state.retry(session);
    assert.equal(state.getSnapshot().status, "rejected");
    assert.equal(state.dismiss(), true);
});

test("same-flight retry is shared, link identities are preserved and duplicate batch rows are rejected", async () => {
    let finish,
        calls = 0;
    const state = controller(async (_session, plan) => {
        calls++;
        if (calls === 1) throw new ApiError("network");
        return new Promise((done) => {
            finish = () => done(receipt(plan));
        });
    });
    const plan = input();
    plan.body.entry = { kind: "link", operation_id: "fictional-existing", expected_version: 4 };
    await assert.rejects(state.start(session, [plan, plan]));
    await state.start(session, [plan]);
    const first = state.retry(session),
        second = state.retry(session);
    assert.equal(first, second);
    await Promise.resolve();
    finish();
    await first;
    assert.equal(state.getSnapshot().receipts[0].operation.id, "fictional-existing");
    assert.equal(state.getSnapshot().receipts[0].operation.version, 4);
});

test("definitive initial rejection permits editing but an intent conflict cannot create a fresh key", async () => {
    for (const code of ["ocr_review_required", "idempotency_conflict"]) {
        const state = controller(async () => {
            throw new ApiError("server", 409, code);
        });
        await state.start(session, [input()]);
        assert.equal(
            state.getSnapshot().status,
            code === "idempotency_conflict" ? "idempotency-conflict" : "rejected",
        );
        assert.equal(state.dismiss(), code !== "idempotency_conflict");
    }
});

test("OCR transport pages explicitly, saves review with PUT, and posts the confirmation envelope", async () => {
    const original = globalThis.fetch,
        calls = [];
    globalThis.fetch = async (path, init) => {
        calls.push([path, init]);
        return new Response("[]", { status: 200, headers: { "content-type": "application/json" } });
    };
    try {
        await api.listOcrDrafts("ledger", {
            job_id: "job",
            status: "draft",
            limit: 25,
            offset: 50,
        });
        await api.saveDraftReview("csrf", "ledger", "draft", { expected_version: 1, review: {} });
        await api.confirmDraft("csrf", "ledger", "draft", {
            ...input().body,
            intent_id: "fictional-intent",
        });
        assert.match(calls[0][0], /limit=25&offset=50&job_id=job&status=draft/u);
        assert.equal(calls[1][1].method, "PUT");
        assert.equal(calls[2][1].method, "POST");
        assert.equal(JSON.parse(calls[2][1].body).intent_id, "fictional-intent");
        assert.throws(() => api.listOcrJobs("ledger", { limit: 201 }), /invalid_pagination/u);
    } finally {
        globalThis.fetch = original;
    }
});
