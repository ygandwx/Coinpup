import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { test } from "node:test";
registerHooks({
    resolve(s, c, next) {
        return next(
            s.startsWith(".") && !/\.[a-z]+$/u.test(s) && c.parentURL?.includes("/src/")
                ? `${s}.ts`
                : s,
            c,
        );
    },
});
const { PendingBusinessConfirmationController } =
    await import("../src/pending-business-confirmation.ts");
const { ApiError } = await import("../src/api.ts");
const load = (n) => JSON.parse(readFileSync(new URL(n, import.meta.url), "utf8"));
const fixture = load("./document-revision-fixture.json"),
    large = load("./document-revision-200-fixture.json");
const sample = (big = false) => structuredClone(big ? large : fixture);
const session = { user: { id: "fictional-owner" }, csrf_token: "fictional-initial" };
const fresh = { ...session, csrf_token: "fictional-fresh" };
function controller(io, read) {
    const c = new PendingBusinessConfirmationController(io, read);
    c.setOwner(session.user.id);
    return c;
}
const start = (c, s, login = session) =>
    c.startRevision(
        login,
        s.review.current.ledger_id,
        s.review.current.document_id,
        s.input,
        s.review,
    );
const draft = (s) => ({
    ...s.review.current.document,
    state: "draft",
    version: s.review.current.version - 1,
});

test("lost response freezes complete200 facts and retries the same bytes/key with fresh CSRF", async () => {
    const s = sample(true),
        expected = JSON.stringify(s.input.body),
        reviewed = JSON.stringify(s.review),
        calls = [];
    const c = controller(async (login, p) => {
        calls.push({ p, csrf: login.csrf_token });
        if (calls.length === 1) throw new ApiError("network");
        return s.receipt;
    });
    const first = start(c, s);
    s.input.body.replacement.lines[199].unit_price = "999";
    s.review.current.document.notes = "Edited after submit";
    await assert.rejects(start(c, sample(true)));
    await first;
    assert.equal(c.getSnapshot().status, "unknown");
    assert.equal(c.dismiss(), false);
    await assert.rejects(c.start(session, draft(sample()), sample().review.oldAsset));
    await c.retry(fresh);
    assert.equal(c.getSnapshot().status, "confirmed");
    assert.equal(c.getSnapshot().receipt, null);
    assert.equal(c.getSnapshot().revisionReceipt.replacement.lines.length, 200);
    assert.equal(calls[0].p, calls[1].p);
    assert.equal(calls[0].p.bodyJson, expected);
    assert.equal(calls[0].p.reviewedJson, reviewed);
    assert.deepEqual(
        calls.map((v) => v.csrf),
        [session.csrf_token, fresh.csrf_token],
    );
    assert.ok(Object.isFrozen(calls[0].p));
    const saved = c.getSnapshot().revisionReceipt;
    assert.ok(
        Object.isFrozen(saved) &&
            Object.isFrozen(saved.previous.lines) &&
            Object.isFrozen(saved.replacement.lines[199].posting),
    );
    s.receipt.replacement.lines[199].posting.operation_id = "mutated transport object";
    assert.notEqual(saved.replacement.lines[199].posting.operation_id, "mutated transport object");
});
test("401 immediately hides private facts and same-owner return requires manual original retry", async () => {
    const s = sample(),
        calls = [];
    const c = controller(async (_login, p) => {
        calls.push(p);
        if (calls.length === 1) throw new ApiError("unauthorized", 401);
        return s.receipt;
    });
    await start(c, s);
    assert.equal(c.getSnapshot().status, "auth-required");
    assert.equal(c.getSnapshot().plan, null);
    assert.equal(c.getSnapshot().revisionReceipt, null);
    assert.equal(c.dismiss(), false);
    c.setOwner(null);
    c.setOwner(session.user.id);
    assert.equal(calls.length, 1);
    assert.equal(c.getSnapshot().status, "unknown");
    await c.retry(fresh);
    assert.equal(c.getSnapshot().status, "confirmed");
    assert.equal(calls[0], calls[1]);
});
test("unknown then version/capacity conflicts never perform GET recovery or discard original intent", async () => {
    for (const code of [
        "version_conflict",
        "document_confirmed",
        "document_revision_capacity",
        "ledger_archived",
    ]) {
        let attempts = 0,
            reads = 0;
        const c = controller(
            async () => {
                if (++attempts === 1) throw new ApiError("network");
                throw new ApiError("server", 409, code);
            },
            async () => {
                reads += 1;
                return fixture.review.current.document;
            },
        );
        await start(c, sample());
        const saved = c.getSnapshot().plan;
        await c.retry(fresh);
        assert.equal(c.getSnapshot().status, "unknown");
        assert.equal(c.getSnapshot().plan, saved);
        assert.equal(reads, 0);
        assert.equal(c.dismiss(), false);
    }
});
test("unambiguous initial denial permits fresh review; idempotency conflict remains blocked", async () => {
    for (const code of ["document_revision_capacity", "idempotency_conflict"]) {
        const c = controller(async () => {
            throw new ApiError("server", 409, code);
        });
        await start(c, sample());
        assert.equal(
            c.getSnapshot().status,
            code === "idempotency_conflict" ? "idempotency-conflict" : "rejected",
        );
        assert.equal(c.dismiss(), code !== "idempotency_conflict");
    }
});
test("malformed final member and later-current read cannot count as success", async () => {
    for (const wrong of [
        () => large.review.current,
        () => {
            const s = sample(true);
            s.receipt.replacement.lines[199].posting.operation_version = 2;
            return s.receipt;
        },
    ]) {
        const c = controller(async () => wrong());
        await start(c, sample(true));
        assert.equal(c.getSnapshot().status, "unknown");
        assert.equal(c.getSnapshot().revisionReceipt, null);
        assert.equal(c.dismiss(), false);
    }
});
test("replacement action and frozen original reason survive cancellation retry", async () => {
    const s = sample();
    s.input.action = s.receipt.action = "cancel";
    delete s.input.body.replacement;
    s.receipt.replacement = null;
    delete s.review.newAsset;
    s.review.oldAsset.enabled = false;
    const calls = [];
    const c = controller(async (_login, p) => {
        calls.push(p);
        if (calls.length === 1) throw new ApiError("network");
        return s.receipt;
    });
    await start(c, s);
    await c.retry(fresh);
    assert.equal(c.getSnapshot().status, "confirmed");
    assert.equal(calls[0], calls[1]);
    assert.equal(calls[1].action, "cancel");
    assert.equal(c.getSnapshot().revisionReceipt.replacement, null);
});
test("both confirmation and revision share one in-flight gate before listener notification", async () => {
    for (const original of [false, true]) {
        let attempts = 0,
            reentered = false;
        const rejected = [];
        const c = controller(async () => {
            attempts += 1;
            throw new ApiError("network");
        });
        c.subscribe(() => {
            if (reentered || c.getSnapshot().status !== "submitting") return;
            reentered = true;
            rejected.push(
                assert.rejects(start(c, sample())),
                assert.rejects(c.start(session, draft(sample()), sample().review.oldAsset)),
            );
        });
        if (original) await c.start(session, draft(sample()), sample().review.oldAsset);
        else await start(c, sample());
        await Promise.all(rejected);
        assert.equal(attempts, 1);
        assert.equal(reentered, true);
        assert.equal(c.getSnapshot().status, "unknown");
    }
});
test("user change and explicit logout discard private intent and isolate late responses", async () => {
    for (const logout of [false, true]) {
        let finish;
        const c = controller(
            () =>
                new Promise((resolve) => {
                    finish = resolve;
                }),
        );
        const flight = start(c, sample());
        await Promise.resolve();
        c.setOwner(logout ? null : "another-owner", logout);
        finish(fixture.receipt);
        await flight;
        assert.equal(c.getSnapshot().status, "idle");
        assert.equal(c.getSnapshot().plan, null);
        assert.equal(c.getSnapshot().revisionReceipt, null);
        await assert.rejects(c.retry(fresh));
    }
});
test("a late old flight cannot clear or overwrite a newer owner's in-flight command", async () => {
    const finishes = [];
    const c = controller(
        () =>
            new Promise((resolve) => {
                finishes.push(resolve);
            }),
    );
    const first = start(c, sample());
    await Promise.resolve();
    const secondSession = { user: { id: "another-owner" }, csrf_token: "fictional-second" };
    c.setOwner(secondSession.user.id);
    const second = start(c, sample(), secondSession);
    await Promise.resolve();
    const active = c.getSnapshot().plan;
    finishes[0](fixture.receipt);
    await first;
    assert.equal(c.getSnapshot().status, "submitting");
    assert.equal(c.getSnapshot().plan, active);
    await assert.rejects(start(c, sample(), secondSession));
    finishes[1](fixture.receipt);
    await second;
    assert.equal(c.getSnapshot().status, "confirmed");
    assert.equal(c.getSnapshot().plan.ownerId, secondSession.user.id);
});
test("invalid review is rejected before allocating or sending a command", async () => {
    let calls = 0;
    const c = controller(async () => {
        calls += 1;
    });
    const s = sample();
    s.review.current.postings.pop();
    await assert.rejects(start(c, s));
    assert.equal(calls, 0);
    assert.equal(c.getSnapshot().plan, null);
});
test("default correction and cancellation transport uses original route/body/key with fresh CSRF", async (t) => {
    const old = globalThis.fetch;
    t.after(() => {
        globalThis.fetch = old;
    });
    for (const cancel of [false, true]) {
        const s = sample(),
            calls = [];
        if (cancel) {
            s.input.action = s.receipt.action = "cancel";
            delete s.input.body.replacement;
            s.receipt.replacement = null;
        }
        globalThis.fetch = async (url, options) => {
            calls.push({ url, options });
            if (calls.length === 1) throw new TypeError("fictional lost response");
            return new Response(JSON.stringify(s.receipt), {
                status: 200,
                headers: { "content-type": "application/json" },
            });
        };
        const c = controller();
        await start(c, s);
        await c.retry(fresh);
        assert.equal(c.getSnapshot().status, "confirmed");
        assert.equal(calls.length, 2);
        assert.ok(calls[0].url.endsWith(cancel ? "/cancellations" : "/corrections"));
        assert.equal(calls[0].url, calls[1].url);
        assert.equal(calls[0].options.method, "POST");
        assert.equal(calls[0].options.credentials, "same-origin");
        assert.equal(calls[0].options.body, JSON.stringify(s.input.body));
        assert.equal(calls[1].options.body, calls[0].options.body);
        const a = new Headers(calls[0].options.headers),
            b = new Headers(calls[1].options.headers);
        assert.equal(a.get("Idempotency-Key"), b.get("Idempotency-Key"));
        assert.equal(a.get("X-CSRF-Token"), session.csrf_token);
        assert.equal(b.get("X-CSRF-Token"), fresh.csrf_token);
    }
});
