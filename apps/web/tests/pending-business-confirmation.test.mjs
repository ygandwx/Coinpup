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
const { PendingBusinessConfirmationController } =
    await import("../src/pending-business-confirmation.ts");
const api = await import("../src/business-confirmation-api.ts");
const id = (n) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const session = { user: { id: "fictional-owner" }, csrf_token: "fictional-old" };
const fresh = { ...session, csrf_token: "fictional-fresh" };
const asset = { asset_id: "USD", scale: 2, enabled: true };
function source() {
    return {
        id: id(1),
        ledger_id: id(2),
        document_kind: "invoice",
        state: "draft",
        archived: false,
        version: 3,
        asset_id: "USD",
        line_count: 3,
        net_amount: "150",
        tax_amount: "15",
        total_amount: "165",
        lines: [
            [2, "100", "10", "110"],
            [4, "0", "0", "0"],
            [7, "50", "5", "55"],
        ].map(([position, net, tax, total]) => ({
            id: id(10 + position),
            document_id: id(1),
            ledger_id: id(2),
            asset_id: "USD",
            archived: false,
            line_no: position,
            net_amount: net,
            tax_amount: tax,
            total_amount: total,
            description: "Fictional service",
        })),
    };
}
function receipt(plan) {
    const body = JSON.parse(plan.bodyJson);
    return {
        kind: "business_confirmation",
        id: body.id,
        ledger_id: plan.ledgerId,
        document_id: plan.documentId,
        source_version: body.expected_version,
        document_version: body.expected_version + 1,
        postings: plan.members.map((member, index) => ({
            ...member,
            operation_id: id(100 + index),
            journal_id: id(200 + index),
        })),
    };
}
function controller(io, read) {
    const result = new PendingBusinessConfirmationController(io, read);
    result.setOwner(session.user.id);
    return result;
}
test("lost response, duplicate click and 401 retain reviewed source and exact intent", async () => {
    const calls = [];
    const value = controller(async (s, plan) => {
        calls.push({ plan, csrf: s.csrf_token });
        if (calls.length === 1) throw new ApiError("network");
        if (calls.length === 2) throw new ApiError("unauthorized", 401);
        return receipt(plan);
    });
    const document = source(),
        reviewed = JSON.stringify(document);
    const first = value.start(session, document, asset);
    document.lines[0].description = "Changed after freeze";
    await assert.rejects(value.start(session, source(), asset));
    await first;
    assert.equal(calls.length, 1);
    assert.equal(value.getSnapshot().status, "unknown");
    assert.equal(value.dismiss(), false);
    await assert.rejects(value.start(session, source(), asset));
    await value.retry(session);
    value.setOwner(null);
    assert.equal(value.getSnapshot().plan, null);
    value.setOwner(session.user.id);
    assert.equal(value.getSnapshot().status, "unknown");
    assert.equal(calls.length, 2);
    await value.retry(fresh);
    const final = value.getSnapshot();
    assert.equal(final.status, "confirmed");
    assert.equal(new Set(calls.map((c) => c.plan)).size, 1);
    assert.equal(final.plan.reviewedJson, reviewed);
    assert.equal(calls[2].csrf, fresh.csrf_token);
    assert.deepEqual(
        final.receipt.postings.map((p) => p.line_no),
        [2, 7],
    );
    assert.ok(Object.isFrozen(final.plan) && Object.isFrozen(final.plan.members[0]));
    assert.ok(
        Object.isFrozen(final.receipt.postings) && Object.isFrozen(final.receipt.postings[0]),
    );
    assert.equal(value.dismiss(), true);
});
test("owner switch and logout discard late responses; temporary signout hides them", async () => {
    for (const mode of ["switch", "logout", "temporary"]) {
        let finish;
        const value = controller(
            (s, plan) =>
                new Promise((resolve) => {
                    finish = () => resolve(receipt(plan));
                }),
        );
        const request = value.start(session, source(), asset);
        await Promise.resolve();
        value.setOwner(mode === "switch" ? "fictional-other" : null, mode === "logout");
        finish();
        await request;
        assert.equal(value.getSnapshot().receipt, null);
        assert.equal(value.getSnapshot().plan, null);
        value.setOwner(session.user.id);
        assert.equal(value.getSnapshot().status, mode === "temporary" ? "confirmed" : "idle");
    }
});
test("every receipt identity and complete ordered posting membership must match", async () => {
    for (const corrupt of [
        (r) => {
            r.kind = "income";
        },
        (r) => {
            r.id = id(99);
        },
        (r) => {
            r.ledger_id = id(99);
        },
        (r) => {
            r.document_id = id(99);
        },
        (r) => {
            r.source_version = 2;
        },
        (r) => {
            r.document_version = 5;
        },
        (r) => {
            r.postings.pop();
        },
        (r) => {
            r.postings.push({ ...r.postings[0] });
        },
        (r) => {
            r.postings.reverse();
        },
        (r) => {
            r.postings[0].line_id = id(99);
        },
        (r) => {
            r.postings[0].line_no = 1;
        },
        (r) => {
            r.postings[0].operation_id = "invalid";
        },
        (r) => {
            r.postings[1].operation_id = r.postings[0].operation_id;
        },
        (r) => {
            r.postings[1].journal_id = r.postings[0].journal_id;
        },
    ]) {
        let first = true;
        const value = controller(async (s, plan) => {
            const result = receipt(plan);
            if (first) {
                corrupt(result);
                first = false;
            }
            return result;
        });
        await value.start(session, source(), asset);
        assert.equal(value.getSnapshot().status, "unknown");
        assert.equal(value.dismiss(), false);
        await value.retry(fresh);
        assert.equal(value.getSnapshot().status, "confirmed");
    }
});
test("known rejection is final only before uncertainty; key conflicts remain blocked", async () => {
    for (const uncertain of [false, true]) {
        let count = 0;
        const value = controller(async () => {
            if (++count === 1 && uncertain) throw new ApiError("network");
            throw new ApiError("server", 409, "period_closed");
        });
        await value.start(session, source(), asset);
        if (uncertain) await value.retry(fresh);
        assert.equal(value.getSnapshot().status, uncertain ? "unknown" : "rejected");
    }
    const value = controller(async () => {
        throw new ApiError("server", 409, "idempotency_conflict");
    });
    await value.start(session, source(), asset);
    assert.equal(value.getSnapshot().status, "idempotency-conflict");
    assert.equal(value.dismiss(), false);
    await assert.rejects(value.start(session, source(), asset));
});
test("only a later owned version fences an uncertain confirmation; GET never means success", async () => {
    for (const code of ["version_conflict", "document_confirmed"]) {
        for (const patch of [
            {},
            { state: "confirmed" },
            { id: id(99) },
            { ledger_id: id(99) },
            { version: 3 },
            { version: "4" },
            { state: "unknown" },
            { version: 2147483648 },
        ]) {
            let count = 0;
            const value = controller(
                async () => {
                    if (++count === 1) throw new ApiError("network");
                    throw new ApiError("server", 409, code);
                },
                async () => ({ ...source(), version: 4, ...patch }),
            );
            await value.start(session, source(), asset);
            await value.retry(fresh);
            assert.equal(
                value.getSnapshot().status,
                Object.keys(patch).length === 0 || patch.state === "confirmed"
                    ? "rejected"
                    : "unknown",
            );
            assert.equal(value.getSnapshot().receipt, null);
        }
    }
    for (const error of [new ApiError("network"), new ApiError("unauthorized", 401)]) {
        let count = 0;
        const value = controller(
            async () => {
                if (++count === 1) throw new ApiError("network");
                throw new ApiError("server", 409, "version_conflict");
            },
            async () => {
                throw error;
            },
        );
        await value.start(session, source(), asset);
        await value.retry(fresh);
        assert.equal(
            value.getSnapshot().status,
            error.status === 401 ? "auth-required" : "unknown",
        );
    }
});
test("invalid reviewed amounts, membership or versions send no command", async () => {
    for (const change of [
        (d) => {
            d.total_amount = 165;
        },
        (d) => {
            d.lines[0].net_amount = "100.001";
        },
        (d) => {
            d.lines[0].net_amount = "-100";
        },
        (d) => {
            d.total_amount = "166";
        },
        (d) => {
            d.lines[1].id = d.lines[0].id;
        },
        (d) => {
            d.lines[1].line_no = 2;
        },
        (d) => {
            d.lines[0].ledger_id = id(99);
        },
        (d) => {
            d.lines[0].asset_id = "BTC";
        },
        (d) => {
            d.version = 2147483647;
        },
        (d) => {
            d.archived = true;
        },
        (d) => {
            d.lines = [];
            d.line_count = 0;
        },
        (d) => {
            d.line_count = 2;
        },
        (d) => {
            d.lines = [d.lines[1]];
            d.line_count = 1;
            d.net_amount = d.tax_amount = d.total_amount = "0";
        },
    ]) {
        const value = controller(async () => {
            assert.fail("invalid input reached transport");
        });
        const document = source();
        change(document);
        await assert.rejects(value.start(session, document, asset), {
            code: "invalid_confirmation_intent",
        });
        assert.equal(value.getSnapshot().status, "idle");
    }
});
test("default transport keeps POST bytes/key and reads draft then sealed version only on 404", async (t) => {
    const original = globalThis.fetch,
        calls = [];
    t.after(() => {
        globalThis.fetch = original;
    });
    globalThis.fetch = async (url, init) => {
        calls.push({ url, ...init });
        if (calls.length === 1) throw new Error("Fictional lost response");
        const status = calls.length === 2 ? 409 : calls.length === 3 ? 404 : 200;
        const body =
            status === 409
                ? { detail: { code: "document_confirmed" } }
                : status === 404
                  ? {}
                  : { document: { ...source(), state: "confirmed", version: 4 } };
        return new Response(JSON.stringify(body), { status });
    };
    const value = controller();
    await value.start(session, source(), asset);
    await value.retry(fresh);
    assert.equal(value.getSnapshot().status, "rejected");
    assert.equal(calls[0].body, calls[1].body);
    assert.equal(calls[0].headers["Idempotency-Key"], calls[1].headers["Idempotency-Key"]);
    assert.equal(calls[1].headers["X-CSRF-Token"], fresh.csrf_token);
    assert.ok(
        calls[0].url.endsWith("/confirm") && calls[2].url.endsWith(`/business-documents/${id(1)}`),
    );
    assert.ok(calls[3].url.endsWith("/confirmation"));
    await api.listBusinessConfirmations(id(2), 25);
    assert.ok(calls[4].url.endsWith("/business-confirmations?limit=25&offset=25"));
    assert.ok(
        calls.every((call) => call.credentials === "same-origin" && call.cache === "no-store"),
    );
});
