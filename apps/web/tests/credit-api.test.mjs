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
const api = await import("../src/credit-api.ts");
const ledger = "fictional-ledger?scope=other",
    credit = "fictional-credit#fragment",
    revision = "fictional-revision/other",
    base = `/api/v1/ledgers/${encodeURIComponent(ledger)}`,
    target = `${base}/business-credits/${encodeURIComponent(credit)}`;
const correction = () => ({
    expected_version: 3,
    reason: "\u0085 Fictional correction 中文 😀 \ufeff",
    replacement: {
        credit_date: "2026-11-07",
        reason: "  Fictional replacement  ",
        lines: [
            {
                source_line_id: "00000000-0000-0000-0000-000000000101",
                net_amount: "99999999999999999999.999999999999999999",
                tax_amount: "0.000000000000000000",
                recognition_date: "2026-11-06",
            },
        ],
    },
});
const cancellation = () => ({ expected_version: 3, reason: " Fictional cancellation 中文 " });
function privateRequest(init) {
    assert.equal(init.credentials, "same-origin");
    assert.equal(init.cache, "no-store");
    assert.equal(init.redirect, "error");
    assert.equal(init.headers.Accept, "application/json");
}

test("revision POST wrappers preserve exact original JSON, path, key and request-local CSRF", async (t) => {
    const calls = [],
        response = { fictional: "transport-only response", amount: "1.000000000000000000" };
    t.mock.method(globalThis, "fetch", async (path, init) => {
        calls.push({ path, init });
        return Response.json(response, { status: 200 });
    });
    for (const [write, suffix, body] of [
        [api.correctCredit, "corrections", correction()],
        [api.cancelCredit, "cancellations", cancellation()],
    ]) {
        const raw = JSON.stringify(body);
        for (const csrf of ["fictional-csrf-first", "fictional-csrf-fresh"]) {
            assert.deepEqual(await write(csrf, ledger, credit, body, "fictional-key"), response);
            const { path, init } = calls.at(-1);
            assert.equal(path, `${target}/${suffix}`);
            assert.equal(init.method, "POST");
            assert.equal(init.body, raw);
            assert.equal(init.headers["Content-Type"], "application/json");
            assert.equal(init.headers["X-CSRF-Token"], csrf);
            assert.equal(init.headers["Idempotency-Key"], "fictional-key");
            privateRequest(init);
        }
        assert.equal(JSON.stringify(body), raw);
    }
    assert.equal(calls.length, 4);
});

test("current and fixed-revision reads do not page members; history alone pages metadata", async (t) => {
    const calls = [],
        members = Array(200).fill("0.000000000000000001"),
        current = { fictional: "current transport-only response", members },
        saved = { fictional: "fixed transport-only response", members },
        history = [{ version: 1, action: "create", revision_id: null }];
    const replies = [current, history, [], saved];
    t.mock.method(globalThis, "fetch", async (path, init) => {
        calls.push({ path, init });
        return Response.json(replies.shift());
    });
    for (const [read, path, response] of [
        [() => api.getCreditCurrent(ledger, credit), `${target}/current`, current],
        [
            () => api.listCreditHistory(ledger, credit),
            `${target}/history?limit=25&offset=0`,
            history,
        ],
        [
            () => api.listCreditHistory(ledger, credit, 25),
            `${target}/history?limit=25&offset=25`,
            [],
        ],
        [
            () => api.getCreditRevision(ledger, credit, revision),
            `${target}/revisions/${encodeURIComponent(revision)}`,
            saved,
        ],
    ]) {
        assert.deepEqual(await read(), response);
        const call = calls.at(-1);
        assert.equal(call.path, path);
        assert.equal(call.init.method, undefined);
        assert.equal(call.init.body, undefined);
        assert.equal(call.init.headers["X-CSRF-Token"], undefined);
        assert.equal(call.init.headers["Idempotency-Key"], undefined);
        privateRequest(call.init);
    }
});

test("every new read forwards cancellation without a retry or fallback request", async (t) => {
    let signal;
    const fetch = t.mock.method(globalThis, "fetch", async (_path, init) => {
        signal = init.signal;
        return new Promise((_resolve, reject) => {
            const abort = () => reject(new DOMException("Fictional abort", "AbortError"));
            if (signal.aborted) abort();
            else signal.addEventListener("abort", abort, { once: true });
        });
    });
    for (const read of [
        (signal) => api.getCreditCurrent(ledger, credit, signal),
        (signal) => api.listCreditHistory(ledger, credit, 50, signal),
        (signal) => api.getCreditRevision(ledger, credit, revision, signal),
    ]) {
        for (const alreadyAborted of [false, true]) {
            const abort = new AbortController();
            if (alreadyAborted) abort.abort();
            const pending = read(abort.signal);
            abort.abort();
            await assert.rejects(pending, { kind: "network" });
            assert.equal(signal.aborted, true);
        }
    }
    assert.equal(fetch.mock.callCount(), 6);
});

test("write errors propagate without consulting current/history or creating another intent", async (t) => {
    const calls = [];
    const fetch = t.mock.method(globalThis, "fetch", async (path) => {
        calls.push(path);
        throw new TypeError("Fictional lost response");
    });
    for (const [write, body] of [
        [api.correctCredit, correction()],
        [api.cancelCredit, cancellation()],
    ]) {
        await assert.rejects(write("fictional-csrf", ledger, credit, body, "fictional-key"), {
            kind: "network",
        });
    }
    for (const [status, code, kind] of [
        [401, "authentication_required", "unauthorized"],
        [409, "version_conflict", "server"],
        [503, "credit_revision_integrity", "server"],
    ]) {
        fetch.mock.mockImplementation(async (path) => {
            calls.push(path);
            return Response.json({ detail: { code } }, { status });
        });
        await assert.rejects(
            api.correctCredit("fictional-csrf", ledger, credit, correction(), "fictional-key"),
            { status, code, kind },
        );
    }
    assert.equal(calls.length, 5);
    assert(calls.every((path) => /\/(corrections|cancellations)$/u.test(path)));
});

test("legacy create, original detail, capacity and list signatures remain unchanged", async (t) => {
    const calls = [],
        body = { id: "fictional-credit", reason: " Fictional original ", lines: [] };
    t.mock.method(globalThis, "fetch", async (path, init) => {
        calls.push({ path, init });
        return Response.json({ fictional: "legacy response" }, { status: init.method ? 201 : 200 });
    });
    await api.createCredit("fictional-csrf", ledger, body, "fictional-key");
    await api.getCredit(ledger, credit);
    await api.getCreditCapacity(ledger, credit);
    await api.getCreditCapacity(ledger, credit, 200);
    await api.listCredits(ledger);
    await api.listCredits(ledger, credit, 25);
    assert.deepEqual(
        calls.map(({ path }) => path),
        [
            `${base}/business-credits`,
            target,
            `${base}/business-documents/${encodeURIComponent(credit)}/credit-capacity?limit=200&offset=0`,
            `${base}/business-documents/${encodeURIComponent(credit)}/credit-capacity?limit=200&offset=200`,
            `${base}/business-credits?limit=25&offset=0`,
            `${base}/business-credits?limit=25&offset=25&document_id=${encodeURIComponent(credit)}`,
        ],
    );
    assert.equal(calls[0].init.body, JSON.stringify(body));
    assert.equal(calls[0].init.method, "POST");
    assert.equal(calls[0].init.headers["Idempotency-Key"], "fictional-key");
});
