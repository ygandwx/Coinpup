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
const {
    originalDocumentFacts,
    validDocumentHistoryEntry,
    validDocumentHistoryPage,
    validDocumentHistoryReceipt,
} = await import("../src/document-history-facts.ts");
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
    const r = f.receipt;
    // The shared command fixture uses sparse source positions independent of a full history.
    // Version9 after original7 represents an already replaced group, whose positions are dense.
    r.previous.lines.forEach((row, i) => {
        row.line_no = i + 1;
    });
    return {
        r,
        scope: {
            ledgerId: r.ledger_id,
            documentId: r.document_id,
            confirmationId: r.confirmation_id,
            originalVersion: 7,
        },
        selected: {
            version: r.version,
            action: r.action,
            revision_id: r.revision_id,
            reason: r.reason,
            created_at: "2026-01-10T12:00:00Z",
            previous_line_count: r.previous.lines.length,
            replacement_line_count: r.replacement.lines.length,
        },
        assets: [f.review.oldAsset, f.review.newAsset],
        current: f.review.current,
    };
}
const check = (s) => validDocumentHistoryReceipt(s.r, s.scope, s.selected, s.assets);
for (const big of [false, true])
    test(`complete fixed receipt display ${big ? 200 : 3}members preserves all raw strings and disabled asset reads`, () => {
        const s = sample(big),
            before = JSON.stringify(s.r);
        s.assets.forEach((a) => (a.enabled = false));
        assert.equal(check(s), true);
        assert.equal(JSON.stringify(s.r), before);
        s.r.action = s.selected.action = "cancel";
        s.r.replacement = null;
        s.selected.replacement_line_count = 0;
        assert.equal(check(s), true);
    });
for (const [label, change] of Object.entries({
    "foreign ledger": (s) => (s.r.ledger_id = id(101)),
    "foreign document": (s) => (s.r.document_id = id(102)),
    "foreign confirmation": (s) => (s.r.confirmation_id = id(103)),
    "wrong fixed revision": (s) => (s.r.revision_id = id(104)),
    "wrong fixed version": (s) => s.r.version++,
    "wrong previous version": (s) => s.r.previous_version--,
    "reason drift": (s) => (s.r.reason += "changed"),
    "action drift": (s) => (s.r.action = "cancel"),
    "extra top field": (s) => (s.r.unexpected = true),
    "missing reversal": (s) => s.r.reversals.pop(),
    "reordered reversals": (s) => s.r.reversals.reverse(),
    "reused reversal journal": (s) =>
        (s.r.reversals[0].journal_id = s.r.previous.lines.find(
            (r) => r.posting,
        ).posting.journal_id),
    "missing final previous line": (s) => s.r.previous.lines.pop(),
    "missing final replacement line": (s) => s.r.replacement.lines.pop(),
    "wrong previous amount": (s) => (s.r.previous.lines.at(-1).total_amount = "99.00"),
    "wrong replacement aggregate": (s) => (s.r.replacement.total_amount = "0.01"),
    "foreign replacement kind": (s) => (s.r.replacement.document_kind = "invoice"),
    "reordered replacement": (s) => s.r.replacement.lines.reverse(),
    "duplicate source": (s) => (s.r.replacement.lines[1].id = s.r.replacement.lines[0].id),
    "invalid date": (s) => (s.r.previous.lines[0].recognition_date = "2026-02-30"),
    "unstripped description": (s) => (s.r.previous.lines[0].description = " padded "),
    "negative amount": (s) => (s.r.previous.net_amount = "-1"),
    "missing asset definition": (s) => s.assets.shift(),
    "ambiguous asset": (s) => s.assets.push({ ...s.assets[0] }),
    "reused new journal": (s) =>
        (s.r.replacement.lines.find((r) => r.posting).posting.journal_id =
            s.r.reversals[0].journal_id),
    "retained operation substituted": (s) => {
        const line = s.r.replacement.lines.find(
            (r) => r.posting && s.r.previous.lines.some((p) => p.id === r.id && p.posting),
        );
        line.posting.operation_id = id(700);
    },
    "retained operation version skipped": (s) =>
        s.r.replacement.lines.find((r) => r.posting).posting.operation_version++,
    "previous operation impossible version": (s) =>
        (s.r.previous.lines.find((r) => r.posting).posting.operation_version = 4),
    "zero line invented posting": (s) => {
        const row = s.r.previous.lines.find((r) => !r.posting);
        row.posting = { operation_id: id(701), operation_version: 1, journal_id: id(702) };
    },
    "null correction replacement": (s) => (s.r.replacement = null),
}))
    test(`${label} is rejected without partial history display`, () => {
        const s = sample();
        change(s);
        assert.equal(check(s), false);
    });
test("last member corruption in200-source fixed receipt is not hidden by first-page validation", () => {
    const s = sample(true);
    s.r.replacement.lines.at(-1).posting.journal_id = s.r.replacement.lines.find(
        (r) => r.posting,
    ).posting.journal_id;
    assert.equal(check(s), false);
});
function entry(scope, n, action = "correct") {
    const version = scope.originalVersion + n;
    return {
        version,
        action: n === 0 ? "confirm" : action,
        revision_id: n === 0 ? null : id(1000 + n),
        reason: n === 0 ? null : "Fictional history",
        created_at: "2026-01-10T00:00:00Z",
        previous_line_count: n === 0 ? null : 3,
        replacement_line_count: n === 0 ? null : action === "cancel" ? 0 : 3,
    };
}
test("history starts at actual confirmation version, including draft versions greater than2", () => {
    const { scope } = sample(),
        first = Array.from({ length: 25 }, (_, i) => entry(scope, i)),
        next = [entry(scope, 25), entry(scope, 26, "cancel")];
    assert.equal(validDocumentHistoryPage(first, scope, 0), true);
    assert.equal(validDocumentHistoryPage(next, scope, 25), true);
    assert.equal(validDocumentHistoryPage([], scope, 50), true);
    assert.equal(validDocumentHistoryPage(next, scope, 0), false);
    assert.equal(validDocumentHistoryPage(first, scope, 1), false);
    first[10].version++;
    assert.equal(validDocumentHistoryPage(first, scope, 0), false);
});
test("confirmation metadata, cancellation position and duplicate revision identities fail closed", () => {
    const { scope } = sample();
    let list = [entry(scope, 0), entry(scope, 1), entry(scope, 2)];
    list[1].action = "cancel";
    assert.equal(validDocumentHistoryPage(list, scope, 0), false);
    list = [entry(scope, 0), entry(scope, 1), entry(scope, 2)];
    list[2].revision_id = list[1].revision_id;
    assert.equal(validDocumentHistoryPage(list, scope, 0), false);
    assert.equal(
        validDocumentHistoryEntry({ ...entry(scope, 0), reason: "not null" }, scope),
        false,
    );
    assert.equal(
        validDocumentHistoryEntry({ ...entry(scope, 1), created_at: "not a timestamp" }, scope),
        false,
    );
    assert.equal(validDocumentHistoryEntry({ ...entry(scope, 1), reason: "\0" }, scope), false);
});
function original(s) {
    const d = s.current.document;
    return {
        document: d,
        confirmation: {
            kind: "business_confirmation",
            id: d.confirmation_id,
            ledger_id: d.ledger_id,
            document_id: d.id,
            source_version: d.version - 1,
            document_version: d.version,
            postings: s.current.postings.map((p) => ({
                line_id: p.source_line_id,
                line_no: d.lines.find((r) => r.id === p.source_line_id).line_no,
                operation_id: p.operation_id,
                journal_id: p.journal_id,
            })),
        },
    };
}
test("original permanent confirmation maps all200lines,including67zero rows,as original v1 accruals", () => {
    const s = sample(true),
        value = original(s),
        before = JSON.stringify(value),
        facts = originalDocumentFacts(value, s.scope.ledgerId, s.scope.documentId, s.assets);
    assert.ok(facts);
    assert.equal(facts.document.lines.length, 200);
    assert.equal(facts.postings.length, 133);
    assert.ok(facts.postings.every((p) => p.operation_version === 1));
    assert.equal(JSON.stringify(value), before);
});
for (const [label, change] of Object.entries({
    "source version": (v) => v.confirmation.source_version--,
    "last posting": (v) => v.confirmation.postings.pop(),
    position: (v) => v.confirmation.postings[0].line_no++,
    scope: (v) => (v.document.ledger_id = id(999)),
    aggregate: (v) => (v.document.tax_amount = "0.01"),
}))
    test(`original ${label} mismatch cannot display`, () => {
        const s = sample(),
            v = original(s);
        change(v);
        assert.equal(
            originalDocumentFacts(v, s.scope.ledgerId, s.scope.documentId, s.assets),
            null,
        );
    });
test("first revision preserves sparse original positions after many saved draft versions", () => {
    const s = sample();
    s.scope.originalVersion = 9;
    const old = new Map();
    s.r.previous.lines.forEach((r, i) => {
        r.line_no = 2 + i * 3;
        if (r.posting) {
            r.posting.operation_version = 1;
            old.set(r.id, r.posting);
        }
    });
    s.r.replacement.lines.forEach((r) => {
        if (r.posting) r.posting.operation_version = old.has(r.id) ? 2 : 1;
    });
    assert.equal(check(s), true);
    s.r.previous.lines.find((r) => r.posting).posting.operation_version = 2;
    assert.equal(check(s), false);
});
test("history detail selection rejects current reads and a mismatched directory entry", () => {
    const s = sample();
    assert.equal(validDocumentHistoryReceipt(s.current, s.scope, s.selected, s.assets), false);
    s.selected = { ...s.selected, reason: "Another fixed revision" };
    assert.equal(check(s), false);
});
test("independent history counts reject omitted zero lines even when prices and journals still match", () => {
    const s = sample();
    const zero = s.r.previous.lines.find((r) => !r.posting);
    s.r.previous.lines = s.r.previous.lines.filter((r) => r !== zero);
    s.r.previous.lines.forEach((r, i) => {
        r.line_no = i + 1;
    });
    assert.equal(check(s), false);
});
test("history count metadata is mandatory, integer and consistent with confirm or cancel", () => {
    const s = sample();
    delete s.selected.previous_line_count;
    assert.equal(check(s), false);
    const { scope } = sample();
    const e = entry(scope, 1);
    for (const n of [null, 0, 201, 1.5, "3"])
        assert.equal(validDocumentHistoryEntry({ ...e, previous_line_count: n }, scope), false);
    assert.equal(
        validDocumentHistoryEntry({ ...e, action: "cancel", replacement_line_count: 3 }, scope),
        false,
    );
    assert.equal(
        validDocumentHistoryEntry({ ...entry(scope, 0), previous_line_count: 3 }, scope),
        false,
    );
});
