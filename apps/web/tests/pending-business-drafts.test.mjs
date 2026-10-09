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
const { PendingBusinessDraftController } = await import("../src/pending-business-drafts.ts");
const { priceBusinessDocument } = await import("../src/business-pricing.ts");
const api = await import("../src/business-draft-api.ts");
const session = { user: { id: "fictional-owner" }, csrf_token: "fictional-old" };
const fresh = { ...session, csrf_token: "fictional-fresh" };
const asset = { asset_id: "fictional-asset", scale: 2 };
const body = () => ({
    id: "fictional-doc",
    document_kind: "invoice",
    party_id: "fictional-party",
    asset_id: asset.asset_id,
    issue_date: "2026-01-01",
    lines: [
        {
            id: "fictional-line",
            description: " Fictional 中文 ",
            quantity: "2.00",
            unit_price: "10.01",
            category_id: "fictional-category",
            recognition_date: "2026-01-01",
        },
    ],
});
function record(plan) {
    const input = JSON.parse(plan.bodyJson),
        source = plan.action === "archive" ? body() : input;
    const price = priceBusinessDocument(source.lines ?? [], plan.scale);
    return {
        ...source,
        id: plan.id,
        ledger_id: plan.ledgerId,
        version: plan.action === "create" ? 1 : input.expected_version + 1,
        state: "draft",
        archived: plan.action === "archive" ? input.archived : false,
        due_date: null,
        notes: null,
        issuer_snapshot: {},
        party_snapshot: {},
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-02T00:00:00Z",
        ...price,
        line_count: price.lines.length,
        lines: (source.lines ?? []).map((line, i) => ({
            discount_amount: "0",
            tax_rate_percent: "0",
            project_id: null,
            ...line,
            description: line.description.trim(),
            ...price.lines[i],
            document_id: plan.id,
            ledger_id: plan.ledgerId,
            asset_id: source.asset_id,
            version: 1,
            archived: false,
            line_no: i + 1,
            category_kind: "income",
            category_snapshot: {},
            project_snapshot: null,
            created_at: "2026-01-01T00:00:00Z",
            updated_at: "2026-01-02T00:00:00Z",
        })),
    };
}
function controller(io, read) {
    const value = new PendingBusinessDraftController(io, read);
    value.setOwner(session.user.id);
    return value;
}
const start = (value, input = body(), action = "create") =>
    value.start(session, "fictional-ledger", "fictional-doc", action, asset, input);
const failure = (status, code) => new ApiError("server", status, code);

test("lost response and 401 retain original line strings and use fresh CSRF; double click is blocked", async () => {
    const calls = [],
        value = controller(async (s, plan) => {
            calls.push({ ...plan, csrf: s.csrf_token });
            if (calls.length === 1) throw new ApiError("network");
            if (calls.length === 2) throw new ApiError("unauthorized", 401);
            return record(plan);
        });
    const input = body(),
        pending = start(value, input);
    input.lines[0].quantity = "999";
    await assert.rejects(start(value));
    await pending;
    assert.equal(value.getSnapshot().status, "unknown");
    assert.equal(value.dismiss(), false);
    await value.retry(session);
    assert.equal(value.getSnapshot().status, "auth-required");
    assert.equal(value.getSnapshot().plan, null);
    assert.equal(value.getSnapshot().record, null);
    value.setOwner(null);
    value.setOwner(session.user.id);
    await value.retry(fresh);
    assert.equal(value.getSnapshot().status, "confirmed");
    assert.equal(value.getSnapshot().record.lines[0].quantity, "2.00");
    assert.equal(new Set(calls.map((call) => call.bodyJson)).size, 1);
    assert.equal(calls[2].csrf, "fictional-fresh");
});

test("create recovery proves only version one with exact input and derived prices", async () => {
    for (const outcome of [
        "same",
        "higher",
        "different",
        "other-ledger",
        "unavailable",
        "missing",
    ]) {
        const value = controller(
            async () => {
                throw failure(409, "duplicate_record");
            },
            async (plan) => {
                if (outcome === "unavailable") throw new ApiError("network");
                if (outcome === "missing") throw failure(404, "not_found");
                const result = record(plan);
                if (outcome === "higher") result.version++;
                if (outcome === "different") result.lines[0].quantity = "2";
                if (outcome === "other-ledger") result.ledger_id = "fictional-other";
                return result;
            },
        );
        await start(value);
        assert.equal(
            value.getSnapshot().status,
            outcome === "same"
                ? "confirmed"
                : ["higher", "different"].includes(outcome)
                  ? "conflict"
                  : "unknown",
            outcome,
        );
    }
});

test("unknown creation can recover after archived entity or disabled asset rejects its retry", async () => {
    for (const code of ["entity_archived", "asset_disabled"]) {
        let calls = 0;
        const value = controller(
            async () => {
                if (++calls === 1) throw new ApiError("network");
                throw failure(409, code);
            },
            async (plan) => record(plan),
        );
        await start(value);
        await value.retry(fresh);
        assert.equal(value.getSnapshot().status, "confirmed");
    }
});

test("unknown save and archive never treat a matching GET as write proof; higher version is conflict", async () => {
    for (const action of ["save", "archive"])
        for (const outcome of ["same", "higher", "new-asset", "read-failed"]) {
            let calls = 0;
            const value = controller(
                async () => {
                    if (++calls === 1) throw new ApiError("network");
                    throw failure(409, "version_conflict");
                },
                async (plan) => {
                    if (outcome === "read-failed") throw new ApiError("network");
                    const result = record(plan);
                    if (outcome === "same") result.version--;
                    if (outcome === "new-asset") {
                        result.asset_id = "fictional-other-asset";
                        result.lines.forEach((line) => {
                            line.asset_id = result.asset_id;
                            line.net_amount = line.total_amount = "0.00000001";
                        });
                        result.net_amount = result.total_amount = "0.00000001";
                    }
                    return result;
                },
            );
            await start(
                value,
                action === "save"
                    ? { ...body(), expected_version: 4 }
                    : { expected_version: 4, archived: true },
                action,
            );
            await value.retry(fresh);
            assert.equal(
                value.getSnapshot().status,
                ["higher", "new-asset"].includes(outcome) ? "conflict" : "unknown",
            );
        }
});

test("valid save matches by stable ID despite retained historical line ordinal; archive checks version and flag", async () => {
    for (const action of ["save", "archive"]) {
        const value = controller(async (_s, plan) => {
            const result = record(plan);
            result.lines[0].line_no = 7;
            return result;
        });
        await start(
            value,
            action === "save"
                ? { ...body(), expected_version: 4 }
                : { expected_version: 4, archived: true },
            action,
        );
        assert.equal(value.getSnapshot().status, "confirmed");
    }
});

test("malformed or mismatched successful responses stay unknown", async () => {
    const changes = [
        (r) => {
            r.ledger_id = "wrong";
        },
        (r) => {
            r.version++;
        },
        (r) => {
            r.total_amount = "20.03";
        },
        (r) => {
            r.lines[0].quantity = "2";
        },
        (r) => {
            r.lines[0].description = "other";
        },
        (r) => {
            r.lines[0].line_no = 2;
        },
        (r) => {
            r.lines.push(r.lines[0]);
            r.line_count++;
        },
        (r) => {
            r.lines = [];
        },
        (r) => {
            r.lines[0].total_amount = "20.03";
        },
        (r) => {
            r.lines[0].archived = true;
        },
        (r) => {
            r.asset_id = "wrong";
        },
        (r) => {
            r.archived = true;
        },
        (r) => {
            r.lines[0].category_kind = "expense";
        },
    ];
    for (const change of changes) {
        const value = controller(async (_s, plan) => {
            const result = record(plan);
            change(result);
            return result;
        });
        await start(value);
        assert.equal(value.getSnapshot().status, "unknown");
        assert.equal(value.getSnapshot().record, null);
    }
});

test("known rejection is terminal but failed retry after uncertainty keeps original intent", async () => {
    for (const status of [400, 403, 404, 409, 422, 429]) {
        const value = controller(async () => {
            throw failure(status, "fictional_error");
        });
        await start(value);
        assert.equal(value.getSnapshot().status, "rejected");
        assert.equal(value.dismiss(), true);
    }
    let calls = 0;
    const value = controller(
        async () => {
            if (++calls === 1) throw new ApiError("network");
            throw failure(422, "fictional_error");
        },
        async () => {
            throw new ApiError("network");
        },
    );
    await start(value);
    const original = value.getSnapshot().plan;
    await value.retry(fresh);
    assert.equal(value.getSnapshot().status, "unknown");
    assert.equal(value.getSnapshot().plan, original);
});

test("owner switch and explicit logout discard intent and ignore late write or conflict reads", async () => {
    for (const read of [false, true])
        for (const logout of [false, true]) {
            let finish, captured;
            const delayed = (plan) => {
                captured = plan;
                return new Promise((resolve) => {
                    finish = resolve;
                });
            };
            const value = controller(async (_s, plan) => {
                if (read) throw failure(409, "duplicate_record");
                return delayed(plan);
            }, delayed);
            const pending = start(value);
            await new Promise((resolve) => setImmediate(resolve));
            value.setOwner(logout ? null : "fictional-other", logout);
            finish(record(captured));
            await pending;
            assert.equal(value.getSnapshot().status, "idle");
            assert.equal(value.getSnapshot().plan, null);
            await assert.rejects(value.retry(session));
        }
});

test("initial version conflict is shown; unauthorized conflict read hides private intent", async () => {
    for (const unauthorized of [false, true]) {
        const value = controller(
            async () => {
                throw failure(409, "version_conflict");
            },
            async (plan) => {
                if (unauthorized) throw new ApiError("unauthorized", 401);
                return record(plan);
            },
        );
        await start(value, { ...body(), expected_version: 4 }, "save");
        assert.equal(value.getSnapshot().status, unauthorized ? "auth-required" : "conflict");
        if (unauthorized) assert.equal(value.getSnapshot().plan, null);
    }
});

test("API wrappers encode scope, preserve strings and use PUT/PATCH with current CSRF", async () => {
    const original = globalThis.fetch,
        calls = [];
    globalThis.fetch = async (url, options) => {
        calls.push({ url, ...options });
        return new Response("{}", { headers: { "Content-Type": "application/json" } });
    };
    try {
        for (const action of ["create", "save", "archive"])
            await api.saveBusinessDraft(
                "fictional-current",
                "fictional/ledger",
                "fictional/doc",
                action,
                body(),
            );
        await api.getBusinessDraft("fictional/ledger", "fictional/doc");
        await api.listBusinessDrafts("fictional/ledger", 25);
        assert.deepEqual(
            calls.slice(0, 3).map((call) => call.method),
            ["POST", "PUT", "PATCH"],
        );
        for (const call of calls.slice(0, 3)) {
            assert.equal(new Headers(call.headers).get("X-CSRF-Token"), "fictional-current");
            assert.equal(JSON.parse(call.body).lines[0].quantity, "2.00");
        }
        assert.ok(calls.every((call) => call.url.includes("fictional%2Fledger")));
        assert.ok(calls[2].url.endsWith("fictional%2Fdoc/archive"));
        assert.ok(calls[4].url.endsWith("limit=25&offset=25&include_archived=true"));
        assert.equal(new Headers(calls[3].headers).has("X-CSRF-Token"), false);
    } finally {
        globalThis.fetch = original;
    }
});
