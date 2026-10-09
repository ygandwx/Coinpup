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
const { draftFields, newDraft, newDraftLine, validDraftFields, validDraftDate } =
    await import("../src/draft-fields.ts");
const refs = () => ({
    assets: [{ asset_id: "fictional:USD", scale: 2, enabled: true }],
    parties: [
        {
            id: "fictional-party",
            name: "Fictional 中文",
            role: "both",
            archived: false,
        },
    ],
    categories: [
        { id: "fictional-income", kind: "income", archived: false },
        { id: "fictional-expense", kind: "expense", archived: false },
    ],
    projects: [{ id: "fictional-project", name: "Fictional project", archived: false }],
});
const input = (kind = "invoice") => ({
    ...newDraft("2026-01-01"),
    document_kind: kind,
    party_id: "fictional-party",
    asset_id: "fictional:USD",
    lines: [
        {
            ...newDraftLine("fictional-line", "2026-01-02"),
            description: " Fictional 中文 ",
            quantity: "2.00",
            unit_price: "10.01",
            category_id: kind === "invoice" ? "fictional-income" : "fictional-expense",
            project_id: "fictional-project",
        },
    ],
});

test("editor projection retains raw sources and removes derived, identity and snapshot output fields", () => {
    const source = {
        ...input(),
        id: "fictional-doc",
        version: 7,
        archived: false,
        ledger_id: "fictional-ledger",
        issuer_snapshot: { name: "Fictional issuer" },
        lines: [
            {
                ...input().lines[0],
                net_amount: "20.02",
                line_no: 9,
                version: 4,
                category_snapshot: {},
            },
        ],
    };
    const fields = draftFields(source);
    assert.deepEqual(fields, input());
    source.lines[0].quantity = "999";
    assert.equal(fields.lines[0].quantity, "2.00");
    assert.equal(fields.lines[0].description, " Fictional 中文 ");
    assert.equal("id" in fields, false);
    assert.equal("net_amount" in fields.lines[0], false);
});

test("new blank and zero line drafts are allowed with valid master references", () => {
    for (const kind of ["invoice", "bill"]) {
        const value = input(kind);
        assert.equal(validDraftFields(value, refs()), true);
        value.lines[0].unit_price = "0";
        assert.equal(validDraftFields(value, refs()), true);
        value.lines = [];
        assert.equal(validDraftFields(value, refs()), true);
    }
    const row = newDraftLine("fictional-stable", "2026-01-31");
    assert.equal(row.id, "fictional-stable");
    assert.equal(row.recognition_date, "2026-01-31");
});

test("save refuses archived or mismatched references while leaving original fields untouched", () => {
    for (const mutate of [
        (r) => {
            r.assets[0].enabled = false;
        },
        (r) => {
            r.parties[0].archived = true;
        },
        (r) => {
            r.parties[0].role = "supplier";
        },
        (r) => {
            r.parties[0].name = null;
        },
        (r) => {
            r.categories[0].kind = "expense";
        },
        (r) => {
            r.categories[0].archived = true;
        },
        (r) => {
            r.projects[0].archived = true;
        },
        (r) => {
            r.projects = [];
        },
    ]) {
        const value = input(),
            before = JSON.stringify(value),
            references = refs();
        mutate(references);
        assert.equal(validDraftFields(value, references), false);
        assert.equal(JSON.stringify(value), before);
    }
});

test("precision, date, duplicate ID and bounded text failures never normalize the source", () => {
    for (const mutate of [
        (v) => {
            v.lines[0].quantity = "1e2";
        },
        (v) => {
            v.lines[0].unit_price = "1.001";
        },
        (v) => {
            v.lines[0].discount_amount = "99";
        },
        (v) => {
            v.lines[0].tax_rate_percent = "1,2";
        },
        (v) => {
            v.lines[0].description = " \u001c ";
        },
        (v) => {
            v.lines[0].description = "Fictional\0";
        },
        (v) => {
            v.lines[0].recognition_date = "2026-02-29";
        },
        (v) => {
            v.issue_date = "0000-01-01";
        },
        (v) => {
            v.due_date = "2026-13-01";
        },
        (v) => {
            v.notes = "n".repeat(2001);
        },
        (v) => {
            v.lines.push(v.lines[0]);
        },
        (v) => {
            v.lines = Array.from({ length: 201 }, (_, i) => ({
                ...v.lines[0],
                id: `fictional-${i}`,
            }));
        },
    ]) {
        const value = input();
        mutate(value);
        const before = JSON.stringify(value);
        assert.equal(validDraftFields(value, refs()), false);
        assert.equal(JSON.stringify(value), before);
    }
    assert.equal(validDraftDate("2000-02-29"), true);
    assert.equal(validDraftDate("1900-02-29"), false);
    assert.equal(validDraftDate("9999-12-31"), true);
    assert.equal(validDraftDate("2026-01-01\n"), false);
});
