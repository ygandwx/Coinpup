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
const { validDocumentRevisionInput, validDocumentRevisionReview } =
    await import("../src/document-revision-intent.ts");
const fixture = JSON.parse(
    readFileSync(new URL("./document-current-fixture.json", import.meta.url), "utf8"),
);
const id = (n) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const headers = ["document_kind", "party_id", "asset_id", "issue_date", "due_date", "notes"];
const fields = [
    "id",
    "description",
    "quantity",
    "unit_price",
    "discount_amount",
    "tax_rate_percent",
    "category_id",
    "project_id",
    "recognition_date",
];
const pick = (v, names) => Object.fromEntries(names.map((n) => [n, v[n]]));
function sample() {
    const { current, asset } = structuredClone(fixture);
    return {
        input: {
            action: "correct",
            body: {
                expected_version: current.version,
                reason: "  Fictional 更正\u0085",
                replacement: {
                    ...pick(current.document, headers),
                    lines: current.document.lines.map((r) => pick(r, fields)),
                    reviewed_references: [],
                },
            },
        },
        review: { current, oldAsset: asset, newAsset: structuredClone(asset), references: [] },
    };
}
const valid = (s) =>
    validDocumentRevisionReview(
        fixture.current.ledger_id,
        fixture.current.document_id,
        s.input,
        s.review,
    );
function pin(s, kind, identity, change = {}) {
    const record = {
        id: identity,
        ledger_id: fixture.current.ledger_id,
        name: "Fictional reviewed 名称",
        version: 6,
        archived: false,
        ...(kind === "party"
            ? {
                  role:
                      s.input.body.replacement.document_kind === "invoice"
                          ? "customer"
                          : "supplier",
              }
            : {}),
        ...change,
    };
    s.input.body.replacement.reviewed_references.push({
        kind,
        id: identity,
        version: record.version,
    });
    s.review.references.push({ kind, record });
}

test("unchanged references retain each old snapshot and input strings without mutation", () => {
    const s = sample(),
        before = JSON.stringify(s);
    assert.equal(validDocumentRevisionInput(s.input), true);
    assert.equal(valid(s), true);
    assert.equal(JSON.stringify(s), before);
    s.input.body.replacement.lines[0].quantity = "1.000000000000000000";
    assert.equal(valid(s), true);
});
test("cancellation keeps the exact reviewed current version and permits disabled historical assets", () => {
    const s = sample();
    s.input = {
        action: "cancel",
        body: { expected_version: 9, reason: "\u001c Fictional 取消\u0085" },
    };
    s.review.oldAsset.enabled = false;
    delete s.review.newAsset;
    s.review.references = [];
    assert.equal(valid(s), true);
    s.input.body.expected_version = 8;
    assert.equal(valid(s), false);
    s.input.body.expected_version = 9;
    s.review.current.status = "cancelled";
    s.review.current.document = null;
    s.review.current.postings = [];
    assert.equal(valid(s), false);
});
test("200 entirely new lines require all selected reference versions and preserve minimum-unit text", () => {
    for (const [asset_id, scale, network, unit] of [
        ["BTC", 8, "bitcoin", "0.00000001"],
        ["ETH", 18, "ethereum", "0.000000000000000001"],
    ]) {
        const s = sample(),
            r = s.input.body.replacement;
        Object.assign(s.review.newAsset, {
            asset_id,
            code: asset_id,
            scale,
            network,
            kind: "native",
        });
        r.asset_id = asset_id;
        const source = r.lines[0];
        r.lines = Array.from({ length: 200 }, (_, i) => ({
            ...source,
            id: id(1000 + i),
            unit_price: unit,
        }));
        assert.equal(valid(s), false);
        pin(s, "category", source.category_id, { kind: "income" });
        pin(s, "project", source.project_id);
        const before = JSON.stringify(s);
        assert.equal(valid(s), true);
        assert.equal(JSON.stringify(s), before);
        r.lines.push({ ...source, id: id(2000) });
        assert.equal(valid(s), false);
    }
});
test("changed or explicitly refreshed party/category/project require matching scoped reviewed data", () => {
    const s = sample(),
        r = s.input.body.replacement;
    r.party_id = id(70);
    r.lines[0].category_id = id(71);
    r.lines[0].project_id = id(72);
    assert.equal(valid(s), false);
    pin(s, "party", id(70));
    pin(s, "category", id(71), { kind: "income" });
    pin(s, "project", id(72));
    assert.equal(valid(s), true);
    for (const change of [
        { version: 7 },
        { ledger_id: id(99) },
        { archived: true },
        { name: "" },
        { role: "supplier" },
        { role: undefined },
    ]) {
        const broken = structuredClone(s);
        Object.assign(broken.review.references[0].record, change);
        assert.equal(valid(broken), false);
    }
    s.review.references[0].record.role = "both";
    assert.equal(valid(s), true);
    s.review.references[1].record.kind = "expense";
    assert.equal(valid(s), false);
    s.review.references[1].record.kind = "income";
    s.review.references.pop();
    assert.equal(valid(s), false);
});
test("all 401 distinct reviewed references fit a complete 200-line replacement", () => {
    const s = sample(),
        r = s.input.body.replacement,
        source = r.lines[0];
    r.party_id = id(5000);
    pin(s, "party", r.party_id);
    r.lines = Array.from({ length: 200 }, (_, i) => ({
        ...source,
        id: id(1000 + i),
        category_id: id(2000 + i),
        project_id: id(3000 + i),
    }));
    for (const row of r.lines) {
        pin(s, "category", row.category_id, { kind: "income" });
        pin(s, "project", row.project_id);
    }
    assert.equal(r.reviewed_references.length, 401);
    assert.equal(valid(s), true);
    s.review.references[400].record.version += 1;
    assert.equal(valid(s), false);
});
test("final positivity follows per-line rounding at the reviewed asset precision", () => {
    const s = sample(),
        r = s.input.body.replacement;
    r.lines = [{ ...r.lines[0], quantity: "0.49", unit_price: "0.01" }];
    assert.equal(validDocumentRevisionInput(s.input), true);
    assert.equal(valid(s), false);
    r.lines[0].quantity = "0.5";
    assert.equal(valid(s), true);
});
test("new source cannot inherit unreviewed snapshots just by reusing a category or project", () => {
    const s = sample(),
        r = s.input.body.replacement;
    r.lines[0].id = id(70);
    assert.equal(valid(s), false);
    pin(s, "category", r.lines[0].category_id, { kind: "income" });
    assert.equal(valid(s), false);
    pin(s, "project", r.lines[0].project_id);
    assert.equal(valid(s), true);
    r.lines.forEach((row) => {
        row.project_id = null;
    });
    assert.equal(valid(s), false); // A pin for an unused reference cannot silently survive.
    r.reviewed_references.pop();
    s.review.references.pop();
    assert.equal(valid(s), true);
});
test("Unicode text follows Python stripping without modifying the command digest input", () => {
    const s = sample();
    for (const accepted of ["\ufeff", "\u0085" + "😀".repeat(1000) + "\u001c", " reason "]) {
        s.input.body.reason = accepted;
        const before = JSON.stringify(s.input);
        assert.equal(valid(s), true);
        assert.equal(JSON.stringify(s.input), before);
    }
    for (const bad of ["\u0085\u001c", "😀".repeat(1001), "x\0y", ""]) {
        s.input.body.reason = bad;
        assert.equal(valid(s), false);
    }
});
test("bill scope and category kind remain fixed while replacement asset may change", () => {
    const s = sample();
    s.review.current.document_kind =
        s.review.current.document.document_kind =
        s.input.body.replacement.document_kind =
            "bill";
    s.review.current.document.lines.forEach((r) => {
        r.category_kind = "expense";
    });
    assert.equal(valid(s), true);
    s.input.body.replacement.party_id = id(70);
    pin(s, "party", id(70));
    assert.equal(valid(s), true);
    s.review.references[0].record.role = "customer";
    assert.equal(valid(s), false);
    s.review.references[0].record.role = "both";
    assert.equal(valid(s), true);
    s.input.body.replacement.document_kind = "invoice";
    assert.equal(valid(s), false);
});
const faults = {
    "stale expected version": (s) => {
        s.input.body.expected_version = 8;
    },
    "maximum non-advancing version": (s) => {
        s.input.body.expected_version = 2147483647;
    },
    "numeric reason": (s) => {
        s.input.body.reason = 1;
    },
    "extra command field": (s) => {
        s.input.body.owner_id = id(99);
    },
    "empty group": (s) => {
        s.input.body.replacement.lines = [];
    },
    "duplicate line": (s) => {
        s.input.body.replacement.lines.push(s.input.body.replacement.lines[0]);
    },
    "numeric price": (s) => {
        s.input.body.replacement.lines[0].unit_price = 1;
    },
    "excess precision": (s) => {
        s.input.body.replacement.lines[0].unit_price = "1.001";
    },
    "zero quantity": (s) => {
        s.input.body.replacement.lines[0].quantity = "0";
    },
    "negative price": (s) => {
        s.input.body.replacement.lines[0].unit_price = "-1";
    },
    "discount above subtotal": (s) => {
        s.input.body.replacement.lines[0].discount_amount = "2";
    },
    "all zero group": (s) => {
        s.input.body.replacement.lines.forEach((r) => {
            r.unit_price = "0";
        });
    },
    "impossible recognition date": (s) => {
        s.input.body.replacement.lines[0].recognition_date = "2026-02-30";
    },
    "blank description": (s) => {
        s.input.body.replacement.lines[0].description = "\u0085";
    },
    "unscoped asset": (s) => {
        s.review.newAsset.asset_id = "EUR";
    },
    "disabled new asset": (s) => {
        s.review.newAsset.enabled = false;
    },
    "missing old asset": (s) => {
        delete s.review.oldAsset;
    },
    "partial current postings": (s) => {
        s.review.current.postings.pop();
    },
    "extra reference": (s) => {
        pin(s, "party", id(99));
    },
    "duplicate pin": (s) => {
        const identity = s.input.body.replacement.party_id;
        pin(s, "party", identity);
        pin(s, "party", identity);
    },
    "pin without actual reviewed data": (s) => {
        s.input.body.replacement.reviewed_references.push({
            kind: "party",
            id: s.input.body.replacement.party_id,
            version: 1,
        });
    },
};
for (const [name, mutate] of Object.entries(faults))
    test(`reject before freezing: ${name}`, () => {
        const s = sample();
        mutate(s);
        assert.equal(valid(s), false);
    });
