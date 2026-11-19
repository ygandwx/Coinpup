import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
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
const { validDocumentCurrent } = await import("../src/document-current-facts.ts");
const { amountFromMinorUnits } = await import("../src/money.ts");
// Produced from the backend's fictional current() fixture and DocumentCurrent schema.
const fixture = JSON.parse(
    readFileSync(new URL("./document-current-fixture.json", import.meta.url), "utf8"),
);
const sample = () => structuredClone(fixture);
const id = (i) => `00000000-0000-0000-0000-${String(i).padStart(12, "0")}`;
const valid = (c, asset) =>
    validDocumentCurrent(c, fixture.current.ledger_id, fixture.current.document_id, asset);

test("original and later current facts preserve sparse positions, zero lines and disabled historical assets", () => {
    const { current, asset } = sample(),
        raw = JSON.stringify(current);
    assert.equal(valid(current, asset), true);
    assert.deepEqual(
        current.document.lines.map((r) => r.line_no),
        [2, 5, 8],
    );
    assert.equal(current.document.lines.length, 3);
    assert.equal(current.postings.length, 2);
    assert.equal(valid(current, { ...asset, enabled: false }), true);
    assert.equal(JSON.stringify(current), raw);
    current.revision_id = null;
    current.postings.forEach((p) => {
        p.operation_version = 1;
    });
    assert.equal(valid(current, asset), true); // Initial confirmed document version is9, not1.
    current.postings[0].operation_version = 2;
    assert.equal(valid(current, asset), false);
});

for (const [code, scale, network] of [
    ["USD", 2, null],
    ["BTC", 8, "bitcoin"],
    ["ETH", 18, "ethereum"],
]) {
    test(`complete200 ${code} current facts preserve every source and smallest-unit accrual`, () => {
        const { current, asset } = sample();
        Object.assign(asset, {
            asset_id: code,
            code,
            scale,
            kind: network ? "native" : "fiat",
            network,
        });
        current.document.asset_id = code;
        const source = current.document.lines[0],
            zero = amountFromMinorUnits(0n, scale),
            unit = amountFromMinorUnits(1n, scale);
        current.document.lines = Array.from({ length: 200 }, (_, i) => ({
            ...source,
            id: id(1000 + i),
            line_no: i + 1,
            asset_id: code,
            quantity: "1.0",
            unit_price: i % 4 ? unit : zero,
            discount_amount: zero,
            tax_rate_percent: "0",
            net_amount: i % 4 ? unit : zero,
            tax_amount: zero,
            total_amount: i % 4 ? unit : zero,
        }));
        current.document.line_count = 200;
        current.document.net_amount = current.document.total_amount = amountFromMinorUnits(
            150n,
            scale,
        );
        current.document.tax_amount = zero;
        current.postings = current.document.lines
            .filter((_, i) => i % 4)
            .map((r, i) => ({
                source_line_id: r.id,
                operation_id: id(2000 + i),
                journal_id: id(3000 + i),
                operation_version: i % 2 ? 1 : 3,
            }));
        const frozen = JSON.stringify(current);
        assert.equal(valid(current, asset), true);
        assert.equal(JSON.stringify(current), frozen);
        current.postings.pop();
        assert.equal(valid(current, asset), false);
    });
}

test("token identity and precision remain specific to the selected network and contract", () => {
    const { current, asset } = sample();
    Object.assign(asset, {
        asset_id: "token:fictional-chain:fictional-contract",
        code: "TEST",
        kind: "token",
        network: "fictional-chain",
        token_reference: "fictional-contract",
    });
    current.document.asset_id = asset.asset_id;
    current.document.lines.forEach((r) => {
        r.asset_id = asset.asset_id;
    });
    assert.equal(valid(current, asset), true);
    for (const change of [
        { token_reference: null },
        { network: "another" },
        { scale: 19 },
        { scale: true },
        { enabled: 1 },
        { asset_id: "token:other:fictional-contract" },
    ])
        assert.equal(valid(current, { ...asset, ...change }), false);
});

const faults = {
    "scope ledger": (c) => {
        c.ledger_id = id(900);
    },
    "scope document": (c) => {
        c.document_id = id(900);
    },
    confirmation: (c) => {
        c.confirmation_id = id(900);
    },
    kind: (c) => {
        c.document_kind = "bill";
    },
    version: (c) => {
        c.version += 1;
    },
    "unsafe version": (c) => {
        c.version = 2147483648;
    },
    "unknown status": (c) => {
        c.status = "draft";
    },
    "draft state": (c) => {
        c.document.state = "draft";
    },
    "archived draft": (c) => {
        c.document.archived = true;
    },
    count: (c) => {
        c.document.line_count += 1;
    },
    "line ledger": (c) => {
        c.document.lines[0].ledger_id = id(900);
    },
    "line document": (c) => {
        c.document.lines[0].document_id = id(900);
    },
    "line asset": (c) => {
        c.document.lines[0].asset_id = "ETH";
    },
    "line kind": (c) => {
        c.document.lines[0].category_kind = "expense";
    },
    "line identity": (c) => {
        c.document.lines[1].id = c.document.lines[0].id;
    },
    "line order": (c) => {
        c.document.lines.reverse();
    },
    "line date": (c) => {
        c.document.lines[0].recognition_date = "2026-02-30";
    },
    "line price": (c) => {
        c.document.lines[0].unit_price = "1.001";
    },
    "numeric money": (c) => {
        c.document.lines[0].unit_price = 1;
    },
    "changed source price": (c) => {
        c.document.lines[0].quantity = "2";
    },
    "changed line total": (c) => {
        c.document.lines[0].total_amount = "2.00";
    },
    "changed header total": (c) => {
        c.document.total_amount = "5.00";
    },
    "missing zero": (c) => {
        c.document.lines.splice(1, 1);
    },
    "posting order": (c) => {
        c.postings.reverse();
    },
    "duplicate operation": (c) => {
        c.postings[1].operation_id = c.postings[0].operation_id;
    },
    "duplicate journal": (c) => {
        c.postings[1].journal_id = c.postings[0].journal_id;
    },
    "posting for zero": (c) => {
        c.postings[0].source_line_id = c.document.lines[1].id;
    },
    "future operation version": (c) => {
        c.postings[0].operation_version = c.version + 1;
    },
    "operation version cannot catch up with confirmed document": (c) => {
        c.postings[0].operation_version = c.version;
    },
    "missing current fact": (c) => {
        c.document = null;
    },
    "extra top-level field": (c) => {
        c.owner_id = id(900);
    },
    "missing asset": (_c, a) => {
        delete a.scale;
    },
};
for (const [name, change] of Object.entries(faults)) {
    test(`inconsistent current facts fail closed: ${name}`, () => {
        const { current, asset } = sample();
        change(current, asset);
        assert.equal(valid(current, asset), false);
    });
}

test("terminal state retains identity without inventing current lines or requiring an active asset", () => {
    const { current } = sample();
    Object.assign(current, { status: "cancelled", version: 10, document: null, postings: [] });
    assert.equal(valid(current), true);
    for (const change of [
        { document: fixture.current.document },
        { postings: fixture.current.postings },
        { revision_id: null },
        { ledger_id: id(900) },
        { version: true },
        { version: 2 },
    ])
        assert.equal(valid({ ...current, ...change }), false);
    for (const bad of [null, [], {}, 3, "document"]) assert.equal(valid(bad), false);
});

test("master snapshots with identities must describe the party, category and project on the current document", () => {
    const { current, asset } = sample();
    const doc = current.document;
    const line = doc.lines.find((row) => row.project_id !== null);
    assert.ok(line);
    doc.party_snapshot.id = doc.party_id;
    doc.party_snapshot.role = "both";
    line.category_snapshot.id = line.category_id;
    line.category_snapshot.kind = line.category_kind;
    line.project_snapshot.id = line.project_id;
    for (const snapshot of [doc.party_snapshot, line.category_snapshot, line.project_snapshot])
        snapshot.version = 1;
    assert.equal(valid(current, asset), true);

    const cases = [
        [doc.party_snapshot, "role", doc.document_kind === "invoice" ? "supplier" : "customer"],
        [doc.party_snapshot, "version", 0],
        [line.category_snapshot, "version", "1"],
        [line.project_snapshot, "version", 2147483648],
        [doc.party_snapshot, "id", id(301)],
        [line.category_snapshot, "id", id(302)],
        [line.project_snapshot, "id", id(303)],
        [line.category_snapshot, "kind", line.category_kind === "income" ? "expense" : "income"],
    ];
    for (const [snapshot, field, other] of cases) {
        const original = snapshot[field];
        snapshot[field] = other;
        assert.equal(valid(current, asset), false, `inconsistent ${field} on ${snapshot.name}`);
        snapshot[field] = original;
    }
    for (const field of ["id", "version"]) {
        const previous = line.project_snapshot[field];
        delete line.project_snapshot[field];
        assert.equal(valid(current, asset), false);
        line.project_snapshot[field] = previous;
    }
    const category = { ...line.category_snapshot };
    const project = { ...line.project_snapshot, id: line.category_id };
    line.project_id = line.category_id;
    line.project_snapshot = project;
    assert.equal(valid(current, asset), true);
    line.category_snapshot = project;
    assert.equal(valid(current, asset), false);
    line.category_snapshot = category;
    line.project_snapshot = category;
    assert.equal(valid(current, asset), false);
    line.project_snapshot = project;
    assert.equal(valid(current, asset), true);
});
