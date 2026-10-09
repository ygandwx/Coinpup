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
const api = await import("../src/recurring-api.ts");
const { PendingRecurringController } = await import("../src/pending-recurring.ts");
const session = {
    user: { id: "fictional-owner" },
    csrf_token: "fictional-old",
};
const fresh = { ...session, csrf_token: "fictional-fresh" };
const body = () => ({
    id: "fictional-rule",
    name: " Fictional 月末 ",
    timezone_name: "Asia/Shanghai",
    anchor_date: "2026-01-31",
    frequency: "month",
    interval_count: 1,
    source_document_id: "fictional-source",
    source_version: 1,
});
function record(plan) {
    const input = JSON.parse(plan.bodyJson),
        values = { ...body(), ...input };
    return {
        ...values,
        id: plan.id,
        ledger_id: plan.ledgerId,
        name: values.name.trim(),
        version: plan.action === "create" ? 1 : input.expected_version + 1,
        archived: plan.action === "archive" ? input.archived : false,
        next_index: 0,
        next_scheduled_date: "2026-01-31",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
        template_input: {
            id: values.source_document_id,
            document_kind: "invoice",
            party_id: "fictional-party",
            asset_id: "USD",
            issue_date: "2026-01-31",
            due_date: null,
            notes: null,
            lines: [
                {
                    id: "fictional-line",
                    description: "Fictional 服务",
                    quantity: "1.00",
                    unit_price: "100.00",
                    discount_amount: "0",
                    tax_rate_percent: "0",
                    category_id: "fictional-category",
                    project_id: null,
                    recognition_date: "2026-01-31",
                },
            ],
        },
    };
}
function controller(io, read) {
    const value = new PendingRecurringController(io, read);
    value.setOwner(session.user.id);
    return value;
}
const start = (value, input = body(), action = "create") =>
    value.start(session, "fictional-ledger", "fictional-rule", action, input);
const failure = (status, code) => new ApiError("server", status, code);

test("lost result and 401 retain original rule and use fresh CSRF, blocking duplicate submissions", async () => {
    const calls = [],
        value = controller(async (s, plan) => {
            calls.push({ ...plan, csrf: s.csrf_token });
            if (calls.length === 1) throw new ApiError("network");
            if (calls.length === 2) throw new ApiError("unauthorized", 401);
            return record(plan);
        });
    const input = body(),
        pending = start(value, input);
    input.frequency = "year";
    await assert.rejects(start(value));
    await pending;
    assert.equal(value.getSnapshot().status, "unknown");
    assert.equal(value.dismiss(), false);
    await value.retry(session);
    assert.equal(value.getSnapshot().status, "auth-required");
    assert.equal(value.getSnapshot().plan, null);
    value.setOwner(null);
    value.setOwner(session.user.id);
    await value.retry(fresh);
    assert.equal(value.getSnapshot().status, "confirmed");
    assert.equal(value.getSnapshot().record.frequency, "month");
    assert.equal(new Set(calls.map((v) => v.bodyJson)).size, 1);
    assert.equal(calls[2].csrf, "fictional-fresh");
});

test("create recovery requires exact initial source, calendar and version before generation", async () => {
    for (const outcome of ["same", "generated", "different", "other-ledger", "read-failed"]) {
        const value = controller(
            async () => {
                throw failure(409, "duplicate_record");
            },
            async (plan) => {
                if (outcome === "read-failed") throw new ApiError("network");
                const result = record(plan);
                if (outcome === "generated") {
                    result.version++;
                    result.next_index++;
                    result.next_scheduled_date = "2026-02-28";
                }
                if (outcome === "different") result.frequency = "year";
                if (outcome === "other-ledger") result.ledger_id = "fictional-other";
                return result;
            },
        );
        await start(value);
        assert.equal(
            value.getSnapshot().status,
            outcome === "same"
                ? "confirmed"
                : ["generated", "different"].includes(outcome)
                  ? "conflict"
                  : "unknown",
            outcome,
        );
    }
});

test("unknown rename, refresh or pause never infer write success from a higher current version", async () => {
    for (const action of ["save", "archive"])
        for (const generated of [false, true]) {
            let calls = 0;
            const value = controller(
                async () => {
                    if (++calls === 1) throw new ApiError("network");
                    throw failure(409, "version_conflict");
                },
                async (plan) => {
                    const result = record(plan);
                    if (generated) {
                        result.next_index++;
                        result.next_scheduled_date = "2026-02-28";
                    } else result.version--;
                    return result;
                },
            );
            await start(
                value,
                action === "save"
                    ? {
                          name: "Fictional 月末",
                          expected_version: 4,
                          source_document_id: "fictional-source",
                          source_version: 1,
                      }
                    : { expected_version: 4, archived: true },
                action,
            );
            await value.retry(fresh);
            assert.equal(value.getSnapshot().status, generated ? "conflict" : "unknown");
        }
});

test("unknown creation remains recoverable when the source or entity is later archived", async () => {
    let calls = 0;
    const value = controller(
        async () => {
            if (++calls === 1) throw new ApiError("network");
            throw failure(409, "entity_archived");
        },
        async (plan) => record(plan),
    );
    await start(value);
    await value.retry(fresh);
    assert.equal(value.getSnapshot().status, "confirmed");
});

test("logout and another owner isolate delayed responses and discard the private intent", async () => {
    for (const explicit of [false, true]) {
        let finish;
        const value = controller(
            async (_session, plan) =>
                new Promise((resolve) => {
                    finish = () => resolve(record(plan));
                }),
        );
        const pending = start(value);
        await Promise.resolve();
        value.setOwner(explicit ? null : "fictional-other", explicit);
        finish();
        await pending;
        assert.equal(value.getSnapshot().status, "idle");
        assert.equal(value.getSnapshot().plan, null);
        await assert.rejects(value.retry(session));
    }
});

test("direct rejection is terminal but malformed responses remain unknown", async () => {
    for (const invalid of [false, true]) {
        const value = controller(async (_session, plan) => {
            if (!invalid) throw failure(422, "recurrence_timezone");
            return { ...record(plan), template_input: null };
        });
        await start(value);
        assert.equal(value.getSnapshot().status, invalid ? "unknown" : "rejected");
        assert.equal(value.dismiss(), !invalid);
    }
});

test("API wrappers encode scope and distinguish rule writes from immutable instance reads", async () => {
    const original = globalThis.fetch,
        calls = [];
    globalThis.fetch = async (url, options) => {
        calls.push({ url, ...options });
        return new Response("{}", { headers: { "Content-Type": "application/json" } });
    };
    try {
        for (const action of ["create", "save", "archive"])
            await api.saveRecurringRule(
                "fictional-current",
                "fictional/ledger",
                "fictional/rule",
                action,
                body(),
            );
        await api.getRecurringRule("fictional/ledger", "fictional/rule");
        await api.listRecurringRules("fictional/ledger", 25);
        await api.listRecurringInstances("fictional/ledger", "fictional/rule", 50);
        assert.deepEqual(
            calls.slice(0, 3).map((call) => call.method),
            ["POST", "PATCH", "PATCH"],
        );
        for (const call of calls.slice(0, 3)) {
            assert.equal(new Headers(call.headers).get("X-CSRF-Token"), "fictional-current");
            assert.equal(JSON.parse(call.body).source_version, 1);
        }
        assert.ok(calls.every((call) => call.url.includes("fictional%2Fledger")));
        assert.ok(calls[2].url.endsWith("fictional%2Frule/archive"));
        assert.ok(calls[4].url.endsWith("limit=25&offset=25&include_archived=true"));
        assert.ok(calls[5].url.endsWith("fictional%2Frule/instances?limit=25&offset=50"));
        assert.ok(calls.slice(3).every((call) => !new Headers(call.headers).has("X-CSRF-Token")));
    } finally {
        globalThis.fetch = original;
    }
});
