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
const api = await import("../src/document-revision-api.ts");
const ledger = "fictional-ledger?other=1",
    document = "fictional-document#fragment",
    revision = "fictional-revision/other";
const target = `/api/v1/ledgers/${encodeURIComponent(ledger)}/business-documents/${encodeURIComponent(document)}`;
const correction = () => ({
    expected_version: 4,
    reason: "  Fictional correction 中文 😀 ",
    replacement: {
        document_kind: "invoice",
        asset_id: "ETH",
        party_id: "fictional-party",
        issue_date: "2026-11-06",
        due_date: null,
        notes: "",
        reviewed_references: [{ kind: "category", id: "fictional-category", version: 3 }],
        lines: Array.from({ length: 200 }, (_, i) => ({
            id: `fictional-source-${i}`,
            description: "Fictional line",
            quantity: "1.00",
            unit_price:
                i === 0 ? "99999999999999999999.999999999999999999" : "0.000000000000000001",
            discount_amount: "0.000000000000000000",
            tax_rate_percent: "0.0",
            category_id: "fictional-category",
            project_id: null,
            recognition_date: "2026-11-06",
        })),
    },
});
const cancellation = () => ({ expected_version: 5, reason: "Fictional cancel" });
function privateRequest(init) {
    assert.equal(init.credentials, "same-origin");
    assert.equal(init.cache, "no-store");
    assert.equal(init.redirect, "error");
}

test("complete document writes preserve every reviewed input, smallest unit and original key", async (t) => {
    const calls = [],
        reply = { fictional: "transport only, not receipt validation" };
    t.mock.method(globalThis, "fetch", async (path, init) => {
        calls.push({ path, init });
        return Response.json(reply);
    });
    for (const [write, suffix, body] of [
        [api.correctDocument, "corrections", correction()],
        [api.cancelDocument, "cancellations", cancellation()],
    ]) {
        const raw = JSON.stringify(body);
        for (const csrf of ["fictional-original-csrf", "fictional-refreshed-csrf"]) {
            assert.deepEqual(
                await write(csrf, ledger, document, body, "fictional-original-key"),
                reply,
            );
            const { path, init } = calls.at(-1);
            assert.equal(path, `${target}/${suffix}`);
            assert.equal(init.method, "POST");
            assert.equal(init.body, raw);
            assert.equal(init.headers["Idempotency-Key"], "fictional-original-key");
            assert.equal(init.headers["X-CSRF-Token"], csrf);
            privateRequest(init);
        }
        assert.equal(JSON.stringify(body), raw);
    }
    assert.equal(calls.length, 4);
});

test("current and fixed details retain complete200 facts; only the directory paginates", async (t) => {
    const calls = [],
        current = { lines: Array(200).fill("0.000000000000000001") },
        fixed = { previous: current, replacement: current };
    const history = [{ version: 4, revision_id: null, action: "confirm", reason: null }];
    const replies = [current, history, [], fixed];
    t.mock.method(globalThis, "fetch", async (path, init) => {
        calls.push({ path, init });
        return Response.json(replies.shift());
    });
    for (const [call, path, expected] of [
        [() => api.getDocumentCurrent(ledger, document), `${target}/current`, current],
        [
            () => api.listDocumentHistory(ledger, document),
            `${target}/history?limit=25&offset=0`,
            history,
        ],
        [
            () => api.listDocumentHistory(ledger, document, 25),
            `${target}/history?limit=25&offset=25`,
            [],
        ],
        [
            () => api.getDocumentRevision(ledger, document, revision),
            `${target}/revisions/${encodeURIComponent(revision)}`,
            fixed,
        ],
    ]) {
        assert.deepEqual(await call(), expected);
        const saved = calls.at(-1);
        assert.equal(saved.path, path);
        assert.equal(saved.init.body, undefined);
        assert.equal(saved.init.headers["Idempotency-Key"], undefined);
        assert.equal(saved.init.headers["X-CSRF-Token"], undefined);
        privateRequest(saved.init);
    }
});

test("read cancellation propagates with exactly one request and no fallback", async (t) => {
    let actual;
    const fetch = t.mock.method(globalThis, "fetch", async (_path, init) => {
        actual = init.signal;
        return new Promise((_resolve, reject) => {
            const abort = () => reject(new DOMException("Fictional abort", "AbortError"));
            if (actual.aborted) abort();
            else actual.addEventListener("abort", abort, { once: true });
        });
    });
    for (const read of [
        (signal) => api.getDocumentCurrent(ledger, document, signal),
        (signal) => api.listDocumentHistory(ledger, document, 25, signal),
        (signal) => api.getDocumentRevision(ledger, document, revision, signal),
    ]) {
        for (const early of [true, false]) {
            const signal = new AbortController();
            if (early) signal.abort();
            const pending = read(signal.signal);
            signal.abort();
            await assert.rejects(pending, { kind: "network" });
            assert.equal(actual.aborted, true);
        }
    }
    assert.equal(fetch.mock.callCount(), 6);
});

test("unknown writes and explicit errors do not generate new intents or infer success from reads", async (t) => {
    let state = "network";
    const calls = [];
    t.mock.method(globalThis, "fetch", async (path) => {
        calls.push(path);
        if (state === "network") throw new TypeError("Fictional response lost");
        return Response.json(
            {
                detail: {
                    code: state === 409 ? "version_conflict" : "not_authenticated",
                    message: "Fictional safe error",
                },
            },
            { status: state },
        );
    });
    for (const [write, suffix, body] of [
        [api.correctDocument, "corrections", correction()],
        [api.cancelDocument, "cancellations", cancellation()],
    ]) {
        for (state of ["network", 401, 409]) {
            await assert.rejects(
                write("fictional-csrf", ledger, document, body, "fictional-stable-key"),
            );
            assert.equal(calls.at(-1), `${target}/${suffix}`);
        }
    }
    assert.equal(calls.length, 6);
});
