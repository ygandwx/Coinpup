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
const { loadCreditRevisionEditor, checkedCreditCurrent } =
    await import("../src/credit-revision-review-loader.ts");
import { fixture, ledger, creditId, documentId, id } from "./credit-revision-fixture.mjs";
function data() {
    const { review } = fixture();
    return {
        review,
        source: {
            document: {
                id: documentId,
                ledger_id: ledger,
                document_kind: "bill",
                asset_id: "USD",
                party_id: review.current.party_id,
                state: "confirmed",
                version: 2,
            },
            confirmation: review.confirmation,
        },
    };
}
function responses(t, value) {
    const calls = [];
    t.mock.method(globalThis, "fetch", async (path, init) => {
        calls.push({ path, init });
        const body = path.endsWith("/current")
            ? value.review.current
            : path.endsWith("/confirmation")
              ? value.source
              : value.review.capacity;
        return Response.json(body);
    });
    return calls;
}
const load = (value, action = "correct", version) =>
    loadCreditRevisionEditor(
        ledger,
        creditId,
        action,
        [value.review.asset],
        new AbortController().signal,
        version,
    );

test("new review loads full matching sources without creating an intent or overwriting group version", async (t) => {
    const value = data(),
        original = JSON.stringify(value),
        calls = responses(t, value);
    const editor = await load(value);
    assert.equal(editor.draft.body.expected_version, 3);
    assert.equal(editor.review.confirmation.document_version, 2);
    assert.equal(editor.draft.body.reason, "");
    assert.equal(editor.draft.body.replacement.reason, "Fictional current");
    assert.equal(editor.draft.body.replacement.lines[0].net_amount, "17.23");
    assert.equal(JSON.stringify(value), original);
    assert.notEqual(editor.review.asset, value.review.asset);
    assert.deepEqual(
        calls.map(({ path }) => path),
        [
            `/api/v1/ledgers/${ledger}/business-credits/${creditId}/current`,
            `/api/v1/ledgers/${ledger}/business-documents/${documentId}/confirmation`,
            `/api/v1/ledgers/${ledger}/business-documents/${documentId}/credit-capacity?limit=200&offset=0`,
        ],
    );
    assert(
        calls.every(
            ({ init }) =>
                init.method === undefined &&
                init.body === undefined &&
                init.cache === "no-store" &&
                init.credentials === "same-origin" &&
                init.headers["Idempotency-Key"] === undefined,
        ),
    );
});

test("disabled-asset cancellation loads immutable sources without reading capacity", async (t) => {
    const value = data();
    value.review.asset.enabled = false;
    delete value.review.capacity;
    const calls = responses(t, value);
    const editor = await load(value, "cancel");
    assert.deepEqual(editor.draft, { action: "cancel", body: { expected_version: 3, reason: "" } });
    assert.equal(editor.review.capacity, undefined);
    assert.equal(editor.review.asset.enabled, false);
    assert.equal(calls.length, 2);
    assert(calls.every(({ path }) => !path.includes("credit-capacity")));
    await assert.rejects(load(value, "correct"), { code: "asset_disabled" });
    assert.equal(calls.length, 3);
});

test("explicit reload refuses a changed group version before fetching sources; terminal groups stay readable", async (t) => {
    const value = data(),
        calls = responses(t, value);
    await assert.rejects(load(value, "correct", 2), { code: "version_conflict" });
    assert.equal(calls.length, 1);
    const terminal = {
        ...value.review.current,
        status: "cancelled",
        credit_date: null,
        credit_reason: null,
        members: [],
    };
    value.review.current = terminal;
    assert.equal(checkedCreditCurrent(terminal, ledger, creditId), terminal);
    await assert.rejects(load(value, "cancel"), { code: "credit_cancelled" });
    assert.equal(calls.length, 2);
    value.review.current = { ...fixture().review.current, version: 2147483647 };
    await assert.rejects(load(value), { code: "credit_revision_version" });
    assert.equal(calls.length, 3);
});

test("source, current, capacity and asset scope or full-member inconsistencies fail before editing", async (t) => {
    let value = data();
    t.mock.method(globalThis, "fetch", async (path) =>
        Response.json(
            path.endsWith("/current")
                ? value.review.current
                : path.endsWith("/confirmation")
                  ? value.source
                  : value.review.capacity,
        ),
    );
    for (const mutate of [
        (v) => (v.review.current.ledger_id = id(999)),
        (v) => (v.review.current.credit_id = id(999)),
        (v) => (v.review.current.document_id = id(999)),
        (v) => (v.review.current.members[0].total_amount = "20.01"),
        (v) => (v.review.current.members[1] = { ...v.review.current.members[0] }),
        (v) => (v.source.document.ledger_id = id(999)),
        (v) => (v.source.document.party_id = id(999)),
        (v) => (v.source.document.document_kind = "invoice"),
        (v) => (v.source.document.asset_id = "EUR"),
        (v) => (v.source.document.state = "draft"),
        (v) => (v.source.document.version = 3),
        (v) => (v.source.confirmation.ledger_id = id(999)),
        (v) => (v.source.confirmation.postings[0].operation_id = id(999)),
        (v) => v.review.capacity.lines.pop(),
        (v) => (v.review.capacity.expected_version = 3),
        (v) => (v.review.capacity.lines[0].credited_net_amount = "16.00"),
        (v) => (v.review.capacity.lines[0].available_gross_amount = "broken"),
        (v) => (v.review.asset.scale = 19),
    ]) {
        value = data();
        mutate(value);
        await assert.rejects(load(value), { code: "invalid_response" });
    }
});

test("summary rejects unsafe or malformed scopes without treating a valid cancelled read as success", () => {
    for (const patch of [
        { ledger_id: id(999) },
        { credit_id: id(999) },
        { status: "unknown" },
        { version: 1.5 },
        { version: 2147483648 },
        { party_id: "broken" },
        { members: [] },
        { credit_date: "2026-02-30" },
        { credit_reason: null },
        { revision_id: null },
        { revision_reason: " " },
        { asset_id: "" },
    ])
        assert.throws(
            () => checkedCreditCurrent({ ...fixture().review.current, ...patch }, ledger, creditId),
            { code: "invalid_response" },
        );
});

test("401 and aborted reads propagate without POST, retry, or a replacement source request", async (t) => {
    const value = data();
    const fetch = t.mock.method(globalThis, "fetch", async () =>
        Response.json({ detail: { code: "authentication_required" } }, { status: 401 }),
    );
    await assert.rejects(load(value), { kind: "unauthorized", status: 401 });
    assert.equal(fetch.mock.callCount(), 1);
    fetch.mock.mockImplementation(async (path, init) => {
        assert.equal(init.method, undefined);
        if (path.endsWith("/current")) return Response.json(value.review.current);
        return new Promise((_resolve, reject) => {
            const abort = () => reject(new DOMException("Fictional cancellation", "AbortError"));
            if (init.signal.aborted) abort();
            else init.signal.addEventListener("abort", abort, { once: true });
        });
    });
    const abort = new AbortController();
    const pending = loadCreditRevisionEditor(
        ledger,
        creditId,
        "correct",
        [value.review.asset],
        abort.signal,
    );
    abort.abort();
    await assert.rejects(pending, { kind: "network" });
    assert(fetch.mock.callCount() <= 4);
});
