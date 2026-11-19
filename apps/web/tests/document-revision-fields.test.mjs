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
const { documentRevisionFields, reviewDocumentRevision } =
    await import("../src/document-revision-fields.ts");
const fixture = JSON.parse(
    readFileSync(new URL("./document-revision-fixture.json", import.meta.url), "utf8"),
);
const id = (n) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
function sample(action = "correct") {
    const { review } = structuredClone(fixture),
        current = review.current,
        doc = current.document;
    const row = (identity) => ({
        id: identity,
        ledger_id: current.ledger_id,
        version: 7,
        name: "Fictional current 名称",
        archived: false,
    });
    const refs = {
        assets: [review.oldAsset, review.newAsset],
        parties: [{ ...row(doc.party_id), role: "supplier" }],
        categories: [{ ...row(doc.lines[0].category_id), kind: "expense" }],
        projects: [{ ...row(doc.lines[0].project_id) }],
    };
    const fields = documentRevisionFields(current, action);
    fields.reason = "  Fictional change\u0085";
    return { current, refs, fields };
}
const build = (s) => reviewDocumentRevision(s.current, s.fields, s.refs);
test("initial fields preserve all current line identities, zero lines and exact input strings", () => {
    const s = sample(),
        fields = s.fields.replacement;
    assert.deepEqual(
        fields.lines.map((r) => r.id),
        s.current.document.lines.map((r) => r.id),
    );
    assert.equal(fields.lines[1].unit_price, "0");
    fields.lines[0].quantity = "9";
    assert.equal(s.current.document.lines[0].quantity, "1");
    assert.throws(() =>
        documentRevisionFields({ ...s.current, status: "cancelled", document: null }, "correct"),
    );
});
test("unchanged references keep their individual historical facts until explicit refresh", () => {
    const s = sample(),
        result = build(s);
    assert.ok(result);
    assert.deepEqual(result.input.body.replacement.reviewed_references, []);
    assert.deepEqual(result.review.references, []);
    assert.equal(
        result.review.current.document.lines[0].category_snapshot.name,
        "Fictional category",
    );
    s.fields.refreshReferences = true;
    const refreshed = build(s);
    assert.equal(refreshed.review.references.length, 3);
    assert.deepEqual(
        refreshed.input.body.replacement.reviewed_references.map((p) => p.kind),
        ["party", "category", "project"],
    );
    assert.ok(refreshed.input.body.replacement.reviewed_references.every((p) => p.version === 7));
});
test("new identity auto-pins its actually selected references once, preserving the shared scope of refresh", () => {
    const s = sample(),
        old = s.fields.replacement.lines[0];
    s.fields.replacement.lines.push({ ...old, id: id(8000) }, { ...old, id: id(8001) });
    const r = build(s);
    assert.ok(r);
    assert.deepEqual(
        r.review.references.map((p) => p.kind),
        ["category", "project"],
    );
    assert.equal(r.input.body.replacement.lines.length, 5);
    assert.equal(r.review.current.document.lines.length, 3);
});
test("200 distinct new sources capture all401 exact party/category/project versions", () => {
    const s = sample(),
        d = s.fields.replacement,
        row = d.lines[0];
    d.party_id = id(7000);
    s.refs.parties.push({ ...s.refs.parties[0], id: d.party_id, version: 8 });
    d.lines = Array.from({ length: 200 }, (_, i) => ({
        ...row,
        id: id(1000 + i),
        category_id: id(2000 + i),
        project_id: id(3000 + i),
    }));
    s.refs.categories = d.lines.map((r, i) => ({
        ...s.refs.categories[0],
        id: r.category_id,
        version: i + 1,
    }));
    s.refs.projects = d.lines.map((r, i) => ({
        ...s.refs.projects[0],
        id: r.project_id,
        version: i + 2,
    }));
    const r = build(s);
    assert.ok(r);
    assert.equal(r.review.references.length, 401);
    assert.equal(r.input.body.replacement.reviewed_references.at(-1).version, 201);
    s.refs.projects[199].ledger_id = id(9000);
    assert.equal(build(s), null);
});
test("cancel uses only exact current facts and permits disabled, missing or archived master catalogs", () => {
    const s = sample("cancel");
    s.refs.assets[0].enabled = false;
    s.refs.categories = [];
    s.refs.parties = [];
    s.refs.projects = [];
    const r = build(s);
    assert.ok(r);
    assert.deepEqual(r.review.references, []);
    assert.equal(r.input.action, "cancel");
    assert.equal(r.review.newAsset, undefined);
    s.fields.replacement = {};
    assert.equal(build(s), null);
});
test("preview snapshot cannot change through later catalog/editor mutation", () => {
    const s = sample();
    s.fields.refreshReferences = true;
    const r = build(s),
        saved = JSON.stringify(r);
    s.refs.categories[0].version += 1;
    s.fields.reason = "different";
    s.current.document.notes = "different";
    assert.equal(JSON.stringify(r), saved);
    assert.notEqual(JSON.stringify(build(s)), saved);
});
const faults = {
    "missing archive state": (s) => {
        delete s.refs.parties[0].archived;
    },
    "non-text current label": (s) => {
        s.refs.projects[0].name = {};
    },
    "changed kind": (s) => {
        s.fields.replacement.document_kind = "invoice";
    },
    "blank reason": (s) => {
        s.fields.reason = "";
    },
    "archived party": (s) => {
        s.refs.parties[0].archived = true;
    },
    "wrong party role": (s) => {
        s.refs.parties[0].role = "customer";
    },
    "foreign party": (s) => {
        s.refs.parties[0].ledger_id = id(9000);
    },
    "missing category": (s) => {
        s.refs.categories = [];
    },
    "duplicate category": (s) => {
        s.refs.categories.push(s.refs.categories[0]);
    },
    "category direction": (s) => {
        s.refs.categories[0].kind = "income";
    },
    "archived old project": (s) => {
        s.refs.projects[0].archived = true;
    },
    "duplicate old asset": (s) => {
        s.refs.assets.push(s.refs.assets[0]);
    },
    "missing new asset": (s) => {
        s.fields.replacement.asset_id = "BTC";
    },
    "disabled target asset": (s) => {
        s.refs.assets[0].enabled = false;
    },
    "empty lines": (s) => {
        s.fields.replacement.lines = [];
    },
    "missing positive current": (s) => {
        s.current.postings.pop();
    },
    "non-advancing version": (s) => {
        s.current.version = s.current.document.version = 2147483647;
    },
};
for (const [name, mutate] of Object.entries(faults))
    test(`cannot prepare review: ${name}`, () => {
        const s = sample();
        mutate(s);
        assert.equal(build(s), null);
    });
