import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";
import { currentRow, id, ledger, creditId, documentId } from "./credit-revision-fixture.mjs";
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
const { readCreditHistory } = await import("../src/credit-revision-history-read.ts");
const endpoint = `/api/v1/ledgers/${ledger}/business-credits/${creditId}`;
const entry = (version, action = "correct") => ({
    version,
    action: version === 1 ? "create" : action,
    revision_id: version === 1 ? null : id(800 + version),
    reason: `Fictional immutable reason ${version} 中文`,
    created_at: "2026-11-07T01:00:00Z",
});
const request = (selected = null, offset = 0) => ({
    ledgerId: ledger,
    creditId,
    documentId,
    assetId: "USD",
    offset,
    selected,
    key: `fictional-owner/${ledger}/${creditId}/${offset}/${selected?.version ?? "page"}/0`,
});
function original() {
    const posting = currentRow(1);
    delete posting.operation_version;
    const fields = {
        id: creditId,
        ledger_id: ledger,
        document_id: documentId,
        asset_id: "USD",
        source_version: 2,
        credit_date: "2026-11-01",
    };
    return {
        ...fields,
        document_kind: "bill",
        party_id: id(6),
        reason: entry(1).reason,
        created_at: entry(1).created_at,
        receipt: { ...fields, kind: "business_credit", postings: [posting] },
    };
}
function revision(selected) {
    const old = currentRow(1),
        added = selected.action === "correct";
    return {
        kind: "business_credit_revision",
        credit_id: creditId,
        ledger_id: ledger,
        document_id: documentId,
        asset_id: "USD",
        revision_id: selected.revision_id,
        version: selected.version,
        previous_version: selected.version - 1,
        action: selected.action,
        reason: selected.reason,
        previous_credit_date: "2026-11-01",
        previous_credit_reason: "Fictional previous reason",
        credit_date: added ? "2026-11-07" : null,
        replacement_reason: added ? "Fictional replacement" : null,
        members: [
            {
                action: selected.action,
                source_line_id: old.source_line_id,
                source_operation_id: old.source_operation_id,
                operation_id: old.operation_id,
                operation_version: selected.version,
                previous_line_no: 1,
                previous_operation_version: selected.version - 1,
                previous_journal_id: old.journal_id,
                reversal_journal_id: id(601),
                previous_net_amount: old.net_amount,
                previous_tax_amount: old.tax_amount,
                previous_total_amount: old.total_amount,
                previous_recognition_date: old.recognition_date,
                line_no: added ? 1 : null,
                journal_id: added ? id(701) : null,
                net_amount: added ? "1.000000000000000000" : null,
                tax_amount: added ? "0.000000000000000000" : null,
                total_amount: added ? "1.000000000000000000" : null,
                recognition_date: added ? "2026-11-07" : null,
            },
        ],
    };
}
function observer() {
    const events = [];
    return {
        events,
        loaded: (v) => events.push(["loaded", v]),
        failed: (e) => events.push(["failed", e]),
        unauthorized: () => events.push(["401"]),
    };
}
const run = (r, out, signal = new AbortController().signal, isCurrent = () => true) =>
    readCreditHistory(r, signal, isCurrent, out);

test("each directory page issues one bounded GET and never eagerly reads members", async (t) => {
    const calls = [];
    t.mock.method(globalThis, "fetch", async (path, init) => {
        calls.push({ path, init });
        const start = Number(new URL(path, "https://coinpup.invalid").searchParams.get("offset"));
        return Response.json(
            Array.from({ length: start ? 1 : 25 }, (_, n) => entry(start + n + 1)),
        );
    });
    for (const offset of [0, 25]) {
        const out = observer(),
            r = request(null, offset),
            before = structuredClone(r);
        await run(r, out);
        assert.equal(out.events.length, 1);
        assert.equal(out.events[0][0], "loaded");
        assert.equal(out.events[0][1].length, offset ? 1 : 25);
        assert.deepEqual(r, before);
    }
    assert.deepEqual(
        calls.map((c) => c.path),
        [0, 25].map((n) => `${endpoint}/history?limit=25&offset=${n}`),
    );
    for (const { init } of calls) {
        assert.equal(init.method, undefined);
        assert.equal(init.body, undefined);
        assert.equal(init.credentials, "same-origin");
        assert.equal(init.cache, "no-store");
        assert(init.signal instanceof AbortSignal);
    }
});

test("create selects the unchanged original detail; corrections and cancellation select one fixed 200", async (t) => {
    let value;
    const calls = [];
    t.mock.method(globalThis, "fetch", async (path) => {
        calls.push(path);
        return Response.json(value);
    });
    for (const selected of [entry(1), entry(2), entry(3, "cancel")]) {
        value = selected.action === "create" ? original() : revision(selected);
        const expected = structuredClone(value),
            r = request(selected),
            out = observer();
        await run(r, out);
        assert.deepEqual(out.events, [["loaded", expected]]);
        assert.deepEqual(value, expected);
        assert.equal(
            calls.at(-1),
            selected.action === "create"
                ? endpoint
                : `${endpoint}/revisions/${selected.revision_id}`,
        );
        if (selected.action !== "create") assert.equal(out.events[0][1].members.length, 1);
    }
    assert.equal(calls.length, 3);
});

test("malformed page and mismatched fixed facts fail closed without a fallback current read", async (t) => {
    let value;
    const fetch = t.mock.method(globalThis, "fetch", async () => Response.json(value));
    for (const [r, result] of [
        [request(), Array.from({ length: 26 }, (_, i) => entry(i + 1))],
        [request(null, 25), [entry(1)]],
        [request(entry(1)), { ...original(), ledger_id: id(999) }],
        [request(entry(2)), { ...revision(entry(2)), revision_id: id(999) }],
        [request(entry(2)), { ...revision(entry(2)), members: [] }],
        [request(entry(3, "cancel")), revision(entry(3))],
    ]) {
        value = result;
        const out = observer(),
            count = fetch.mock.callCount();
        await run(r, out);
        assert.equal(out.events.length, 1);
        assert.equal(out.events[0][0], "failed");
        assert.equal(out.events[0][1].code, "invalid_response");
        assert.equal(fetch.mock.callCount(), count + 1);
    }
});

test("invalid scope, page or selected identity is rejected before any HTTP request", async (t) => {
    const fetch = t.mock.method(globalThis, "fetch", async () => assert.fail("Unexpected read"));
    for (const patch of [
        { ledgerId: "bad" },
        { creditId: "bad" },
        { documentId: "bad" },
        { assetId: "" },
        ...[-25, 1, 1.5, 100025].map((offset) => ({ offset })),
        { selected: { ...entry(1), action: "correct" } },
        { selected: { ...entry(2), revision_id: null } },
        { selected: { ...entry(2), version: 2147483648 } },
    ]) {
        const out = observer();
        await run({ ...request(), ...patch }, out);
        assert.equal(out.events[0][1].code, "invalid_response");
    }
    assert.equal(fetch.mock.callCount(), 0);
});

test("current 401 publishes only its error and authorization event; other failures keep their identity", async (t) => {
    let status = 401;
    const fetch = t.mock.method(globalThis, "fetch", async () =>
        Response.json({ detail: { code: "fictional_failure" } }, { status }),
    );
    for (status of [401, 403, 503]) {
        const out = observer();
        await run(request(), out);
        assert.equal(out.events[0][0], "failed");
        assert.equal(out.events[0][1].status, status);
        assert.equal(out.events[0][1].code, "fictional_failure");
        assert.deepEqual(out.events.slice(1), status === 401 ? [["401"]] : []);
    }
    assert.equal(fetch.mock.callCount(), 3);
});

test("the caller identity gate suppresses late responses even when a newer session reads the same endpoint", async (t) => {
    let finish;
    t.mock.method(
        globalThis,
        "fetch",
        async (path) =>
            new Promise((resolve, reject) => {
                finish = { resolve, reject, path };
            }),
    );
    for (const [change, nextRead, nextValue] of [
        ["same endpoint, new session", request(), [entry(1)]],
        ["owner", { ...request(), key: "fictional-other-owner" }, [entry(1)]],
        ["ledger", { ...request(), ledgerId: id(998) }, [entry(1)]],
        ["credit", { ...request(), creditId: id(999) }, [entry(1)]],
        ["page", request(null, 25), [entry(26)]],
        ["selection", request(entry(2)), revision(entry(2))],
        ["refresh", { ...request(), key: "fictional-refresh-1" }, [entry(1)]],
    ])
        for (const outcome of ["success", "malformed", "401", "503", "network"]) {
            const old = { change, generation: 1 },
                out = observer();
            let active = old;
            const pending = run(request(), out, new AbortController().signal, () => active === old);
            const oldResponse = finish;
            active = { change, generation: 2 };
            const next = active;
            const latest = run(nextRead, out, new AbortController().signal, () => active === next);
            assert.equal(
                finish.path,
                `/api/v1/ledgers/${nextRead.ledgerId}/business-credits/${nextRead.creditId}` +
                    (nextRead.selected
                        ? `/revisions/${nextRead.selected.revision_id}`
                        : `/history?limit=25&offset=${nextRead.offset}`),
            );
            finish.resolve(Response.json(nextValue));
            await latest;
            const saved = [...out.events];
            if (outcome === "network") oldResponse.reject(new TypeError("Fictional interruption"));
            else
                oldResponse.resolve(
                    outcome === "success"
                        ? Response.json([entry(1)])
                        : outcome === "malformed"
                          ? new Response("{")
                          : Response.json(
                                { detail: "Fictional old session" },
                                { status: Number(outcome) },
                            ),
                );
            await pending;
            assert.deepEqual(out.events, saved, `${change}/${outcome}`);
            assert.deepEqual(out.events, [["loaded", nextValue]]);
        }
});

test("abort and unavailable ownership stop reads, including fetch implementations which ignore abort", async (t) => {
    let finish;
    const fetch = t.mock.method(
        globalThis,
        "fetch",
        async () =>
            new Promise((resolve) => {
                finish = resolve;
            }),
    );
    const out = observer(),
        abort = new AbortController();
    const pending = run(request(), out, abort.signal);
    abort.abort();
    finish(Response.json({ detail: "Fictional obsolete 401" }, { status: 401 }));
    await pending;
    await run(request(), out, abort.signal);
    await run(request(), out, new AbortController().signal, () => false);
    assert.equal(fetch.mock.callCount(), 1);
    assert.deepEqual(out.events, []);
});
