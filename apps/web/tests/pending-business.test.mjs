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
const { PendingMasterDataController, masterName } = await import("../src/pending-business.ts");
const api = await import("../src/business-api.ts");
const session = {
    user: { id: "fictional-owner" },
    csrf_token: "fictional-old",
};
const fresh = { ...session, csrf_token: "fictional-fresh" };
const other = {
    user: { id: "fictional-other" },
    csrf_token: "fictional-other",
};
const body = () => ({
    id: "fictional-record",
    name: " Fictional 中文 ",
    role: "both",
    notes: "  Fictional note  ",
});
function record(plan) {
    const input = JSON.parse(plan.bodyJson);
    const result = {
        id: plan.id,
        ledger_id: plan.ledgerId,
        version: plan.method === "POST" ? 1 : input.expected_version + 1,
        archived: false,
        name: "Fictional existing",
        notes: null,
        ...(plan.kind === "parties"
            ? {
                  role: "both",
                  legal_name: null,
                  email: null,
                  phone: null,
                  address: null,
                  tax_identifier: null,
              }
            : {}),
        ...input,
        created_at: "2026-01-01T12:00:00Z",
        updated_at: "2026-01-02T12:00:00Z",
    };
    delete result.expected_version;
    if (typeof result.name === "string") result.name = masterName(result.name);
    if (typeof result.legal_name === "string") result.legal_name = masterName(result.legal_name);
    return result;
}
function controller(io, read) {
    const value = new PendingMasterDataController(io, read);
    value.setOwner(session.user.id);
    return value;
}
const start = (value, input = body()) =>
    value.start(session, "fictional-ledger", "parties", input.id, "POST", input);

test("lost response, 401 and same-owner retry retain the original JSON and identity", async () => {
    const calls = [];
    const value = controller(async (s, plan) => {
        calls.push({ ...plan, csrf: s.csrf_token });
        if (calls.length === 1) throw new ApiError("network");
        if (calls.length === 2) throw new ApiError("unauthorized", 401);
        return record(plan);
    });
    const input = body(),
        promise = start(value, input);
    input.name = "Changed after freezing";
    await assert.rejects(start(value));
    await promise;
    assert.equal(value.getSnapshot().status, "unknown");
    assert.equal(value.dismiss(), false);
    await value.retry(session);
    assert.equal(value.getSnapshot().status, "auth-required");
    assert.equal(value.getSnapshot().plan, null);
    value.setOwner(null);
    value.setOwner(session.user.id);
    assert.equal(value.getSnapshot().status, "unknown");
    await value.retry(fresh);
    assert.equal(value.getSnapshot().status, "confirmed");
    assert.equal(new Set(calls.map((call) => call.bodyJson)).size, 1);
    assert.equal(calls[2].csrf, "fictional-fresh");
    assert.equal(value.getSnapshot().record.name, "Fictional 中文");
});

test("duplicate create is confirmed only by matching version-one contents", async () => {
    for (const patch of [{}, { version: 2 }, { name: "Fictional different" }]) {
        const changed = Object.keys(patch).length > 0;
        const value = controller(
            async () => {
                throw new ApiError("server", 409, "duplicate_record");
            },
            async (plan) => ({ ...record(plan), ...patch }),
        );
        await start(value);
        assert.equal(value.getSnapshot().status, changed ? "conflict" : "confirmed");
        assert.equal(value.getSnapshot().plan.id, "fictional-record");
        if (!changed) assert.equal(value.getSnapshot().error, null);
        assert.equal(value.dismiss(), true);
    }
});

test("unknown updates need a higher scoped version and never claim their matching edit succeeded", async () => {
    for (const outcome of ["higher", "same", "other-ledger", "unavailable"]) {
        let calls = 0;
        const value = controller(
            async () => {
                if (++calls === 1) throw new ApiError("network");
                throw new ApiError("server", 409, "version_conflict");
            },
            async (plan) => {
                if (outcome === "unavailable") throw new ApiError("network");
                return {
                    ...record(plan),
                    ...(outcome === "same" ? { version: 1 } : {}),
                    ...(outcome === "other-ledger" ? { ledger_id: "fictional-wrong" } : {}),
                };
            },
        );
        const input = { expected_version: 1, name: "Fictional updated" };
        await value.start(
            session,
            "fictional-ledger",
            "projects",
            "fictional-record",
            "PATCH",
            input,
        );
        await value.retry(session);
        assert.equal(value.getSnapshot().status, outcome === "higher" ? "conflict" : "unknown");
        assert.equal(value.getSnapshot().plan.bodyJson, JSON.stringify(input));
        assert.equal(value.dismiss(), outcome === "higher");
    }
});

test("invalid success responses remain unknown without replacing the plan", async () => {
    for (const patch of [
        { ledger_id: "fictional-wrong" },
        { id: "fictional-wrong" },
        { version: 9 },
        { name: "Fictional wrong" },
        { notes: "trimmed" },
        { archived: true },
        { updated_at: "bad date" },
    ]) {
        const value = controller(async (_s, plan) => ({
            ...record(plan),
            ...patch,
        }));
        await start(value);
        assert.equal(value.getSnapshot().status, "unknown");
        assert.equal(value.getSnapshot().error.code, "invalid_response");
    }
});

test("user switch and explicit logout fence late responses", async () => {
    for (const logout of [false, true]) {
        let release, entered;
        const ready = new Promise((resolve) => {
            entered = resolve;
        });
        const value = controller(
            (_s, plan) =>
                new Promise((resolve) => {
                    release = () => resolve(record(plan));
                    entered();
                }),
        );
        const pending = start(value);
        await ready;
        await assert.rejects(value.retry(session));
        assert.equal(value.dismiss(), false);
        value.setOwner(logout ? null : other.user.id, logout);
        release();
        await pending;
        assert.equal(value.getSnapshot().plan, null);
        assert.equal(value.getSnapshot().record, null);
        assert.equal(value.getSnapshot().status, "idle");
    }
});

test("known validation rejection permits correction, but rejection after uncertainty does not", async () => {
    for (const unknown of [false, true]) {
        let calls = 0;
        const value = controller(async () => {
            if (unknown && ++calls === 1) throw new ApiError("network");
            throw new ApiError("server", 422, "party_profile_incomplete");
        });
        await start(value);
        if (unknown) await value.retry(session);
        assert.equal(value.getSnapshot().status, unknown ? "unknown" : "rejected");
        assert.equal(value.dismiss(), !unknown);
    }
});

test("names follow Python strip while other submitted text remains exact", () => {
    assert.equal(masterName("\u0085\u001fFictional\u00a0"), "Fictional");
    assert.equal(masterName("\ufeffFictional\ufeff"), "\ufeffFictional\ufeff");
});

test("master data transport uses CSRF and no financial idempotency key", async () => {
    const original = globalThis.fetch,
        calls = [];
    globalThis.fetch = async (url, options) => {
        calls.push({ url, options });
        return new Response("{}", {
            status: 200,
            headers: { "content-type": "application/json" },
        });
    };
    try {
        await api.saveMasterData(
            "fictional-csrf",
            "fictional-ledger",
            "projects",
            "fictional-record",
            "PATCH",
            { expected_version: 1, notes: null },
        );
        assert.equal(
            calls[0].url,
            "/api/v1/ledgers/fictional-ledger/business-projects/fictional-record",
        );
        assert.equal(calls[0].options.method, "PATCH");
        assert.equal(new Headers(calls[0].options.headers).get("x-csrf-token"), "fictional-csrf");
        assert.equal(new Headers(calls[0].options.headers).get("idempotency-key"), null);
    } finally {
        globalThis.fetch = original;
    }
});
