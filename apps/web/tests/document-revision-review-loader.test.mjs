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
const { loadDocumentRevisionEditor, checkedDocumentCurrent } =
    await import("../src/document-revision-review-loader.ts");
const { reviewDocumentRevision } = await import("../src/document-revision-fields.ts");
const id = (n) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
function sample(big = false) {
    const f = JSON.parse(
        readFileSync(
            new URL(
                big ? "./document-revision-200-fixture.json" : "./document-revision-fixture.json",
                import.meta.url,
            ),
            "utf8",
        ),
    );
    const current = f.review.current,
        d = current.document;
    const common = (identity) => ({
        id: identity,
        ledger_id: current.ledger_id,
        version: 7,
        name: "Fictional 资料",
        archived: false,
    });
    return {
        current,
        assets: [f.review.oldAsset, f.review.newAsset],
        categories: [{ ...common(d.lines[0].category_id), kind: "expense" }],
        parties: [{ ...common(d.party_id), role: "supplier" }],
        projects: d.lines[0].project_id ? [common(d.lines[0].project_id)] : [],
    };
}
function respond(t, s, override) {
    const calls = [];
    t.mock.method(globalThis, "fetch", async (path, init) => {
        calls.push({ path, init });
        if (override) {
            const r = await override(path, init);
            if (r) return r;
        }
        const u = new URL(path, "https://coinpup.invalid"),
            offset = Number(u.searchParams.get("offset"));
        return Response.json(
            path.endsWith("/current")
                ? s.current
                : (path.includes("business-parties") ? s.parties : s.projects).slice(
                      offset,
                      offset + 25,
                  ),
        );
    });
    return calls;
}
const load = (s, action = "correct", signal = new AbortController().signal, version) =>
    loadDocumentRevisionEditor(
        s.current.ledger_id,
        s.current.document_id,
        action,
        s.assets,
        s.categories,
        signal,
        version,
    );
test("complete200-line review preserves raw inputs and performs only scoped no-store reads", async (t) => {
    const s = sample(true),
        calls = respond(t, s),
        editor = await load(s);
    assert.equal(editor.current.document.lines.length, 200);
    assert.deepEqual(
        editor.fields.replacement.lines.map((r) => r.unit_price),
        s.current.document.lines.map((r) => r.unit_price),
    );
    assert.equal(editor.fields.reason, "");
    assert.notEqual(editor.refs.assets[0], s.assets[0]);
    assert.ok(
        calls.every(
            ({ init }) =>
                init.method === undefined &&
                !init.body &&
                init.cache === "no-store" &&
                init.credentials === "same-origin" &&
                !init.headers["Idempotency-Key"],
        ),
    );
    assert.equal(calls.length, 3);
});
test("catalog paging reads all pages including exact page boundary", async (t) => {
    const s = sample();
    s.parties = Array.from({ length: 50 }, (_, n) => ({ ...s.parties[0], id: id(10000 + n) }));
    s.projects = Array.from({ length: 26 }, (_, n) => ({ ...s.projects[0], id: id(20000 + n) }));
    const calls = respond(t, s),
        e = await load(s);
    assert.equal(e.refs.parties.length, 50);
    assert.equal(e.refs.projects.length, 26);
    assert.deepEqual(
        calls
            .filter((c) => c.path.includes("business-parties"))
            .map((c) => new URL(c.path, "https://x.invalid").searchParams.get("offset")),
        ["0", "25", "50"],
    );
});
test("cancel ignores missing masters and disabled old asset", async (t) => {
    const s = sample();
    s.assets[0].enabled = false;
    s.categories = [];
    s.parties = [];
    s.projects = [];
    const calls = respond(t, s),
        e = await load(s, "cancel");
    assert.equal(calls.length, 1);
    assert.equal(e.fields.replacement, null);
    e.fields.reason = "Fictional cancellation";
    assert.ok(reviewDocumentRevision(e.current, e.fields, e.refs));
});
for (const [label, change] of [
    ["foreign ledger", (s) => (s.current.ledger_id = id(987))],
    ["missing last posting", (s) => s.current.postings.pop()],
    ["inconsistent last amount", (s) => (s.current.document.lines.at(-1).total_amount = "9.999")],
    ["ambiguous asset", (s) => s.assets.push({ ...s.assets[0] })],
])
    test(`${label} fails closed before catalogs`, async (t) => {
        const s = sample(),
            ledger = s.current.ledger_id;
        change(s);
        const calls = respond(t, s);
        await assert.rejects(
            loadDocumentRevisionEditor(
                ledger,
                s.current.document_id,
                "correct",
                s.assets,
                s.categories,
                new AbortController().signal,
            ),
            { code: "invalid_response" },
        );
        assert.equal(calls.length, 1);
    });
for (const [label, change] of [
    ["foreign master", (s) => (s.parties[0].ledger_id = id(900))],
    ["duplicate master", (s) => s.parties.push({ ...s.parties[0] })],
    ["invalid role", (s) => (s.parties[0].role = "other")],
    ["invalid version", (s) => (s.projects[0].version = 0)],
    ["foreign category", (s) => (s.categories[0].ledger_id = id(900))],
    ["duplicate category", (s) => s.categories.push({ ...s.categories[0] })],
])
    test(`${label} is rejected`, async (t) => {
        const s = sample();
        change(s);
        respond(t, s);
        await assert.rejects(load(s), { code: "invalid_response" });
    });
test("terminal current remains readable but cannot open an editor", async (t) => {
    const s = sample();
    s.current = {
        ...s.current,
        version: 3,
        status: "cancelled",
        document: null,
        postings: [],
        revision_id: id(900),
    };
    assert.equal(
        checkedDocumentCurrent(s.current, s.current.ledger_id, s.current.document_id, []),
        s.current,
    );
    const calls = respond(t, s);
    await assert.rejects(load(s, "cancel"), { code: "document_cancelled" });
    assert.equal(calls.length, 1);
});
test("explicit preserved editor version refuses drift instead of replacing old inputs", async (t) => {
    const s = sample(),
        calls = respond(t, s);
    await assert.rejects(load(s, "correct", new AbortController().signal, s.current.version + 1), {
        code: "version_conflict",
    });
    assert.equal(calls.length, 1);
});
test("pre-abort makes no request; abort after current read never loads private catalogs", async (t) => {
    const s = sample(),
        abort = new AbortController();
    let calls = respond(t, s);
    abort.abort();
    await assert.rejects(load(s, "correct", abort.signal), { name: "AbortError" });
    assert.equal(calls.length, 0);
    t.mock.restoreAll();
    const later = new AbortController();
    calls = respond(t, s, () => {
        later.abort();
    });
    await assert.rejects(load(s, "correct", later.signal), { name: "AbortError" });
    assert.equal(calls.length, 1);
});
test("401 propagates without loading masters or creating a write", async (t) => {
    const s = sample(),
        calls = respond(t, s, () =>
            Response.json({ detail: { code: "unauthorized" } }, { status: 401 }),
        );
    await assert.rejects(load(s), { status: 401 });
    assert.equal(calls.length, 1);
});
test("assets and categories are captured before awaiting mutable external data", async (t) => {
    const s = sample();
    respond(t, s, (path) => {
        if (path.endsWith("/current")) {
            s.assets[0].scale = 18;
            s.categories[0].name = "Later mutation";
        }
    });
    const e = await load(s);
    assert.equal(e.refs.assets[0].scale, 2);
    assert.equal(e.refs.categories[0].name, "Fictional 资料");
});
