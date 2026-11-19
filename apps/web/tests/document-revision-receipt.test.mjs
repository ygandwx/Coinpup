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
const { validDocumentRevisionReceipt } = await import("../src/document-revision-receipt.ts");
// Exported from backend sources_and_receipt(), then validated by DocumentRevisionResponse.
// The 200 fixture has 400 distinct source lines and 133 old / 200 new actual posting identities.
const load = (name) => JSON.parse(readFileSync(new URL(name, import.meta.url), "utf8"));
const fixture = load("./document-revision-fixture.json"),
    large = load("./document-revision-200-fixture.json");
const sample = (many = false) => structuredClone(many ? large : fixture);
const id = (n) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const plan = (s) => ({
    ownerId: "1",
    ledgerId: s.review.current.ledger_id,
    documentId: s.review.current.document_id,
    action: s.input.action,
    key: id(9000),
    bodyJson: JSON.stringify(s.input.body),
    reviewedJson: JSON.stringify(s.review),
});
const valid = (s) => validDocumentRevisionReceipt(s.receipt, plan(s));
test("actual backend cross-asset receipt proves sparse old facts, zero-to-positive, reordered retained accruals and exact text", () => {
    const s = sample(),
        before = JSON.stringify(s);
    assert.equal(valid(s), true);
    assert.equal(JSON.stringify(s), before);
    assert.deepEqual(
        s.receipt.previous.lines.map((r) => r.line_no),
        [2, 5, 8],
    );
    assert.equal(s.receipt.previous.lines[1].posting, null);
    assert.equal(s.receipt.replacement.lines[1].posting.operation_version, 1);
});
test("complete 200-to-200 exchange checks every old zero, reversal and final new posting", () => {
    const s = sample(true),
        before = JSON.stringify(s);
    assert.equal(s.receipt.previous.lines.length, 200);
    assert.equal(s.receipt.replacement.lines.length, 200);
    assert.equal(s.receipt.reversals.length, 133);
    assert.equal(
        new Set([...s.receipt.previous.lines, ...s.receipt.replacement.lines].map((r) => r.id))
            .size,
        400,
    );
    assert.equal(valid(s), true);
    assert.equal(JSON.stringify(s), before);
    s.receipt.replacement.lines[199].posting.operation_id =
        s.receipt.previous.lines[199].posting.operation_id;
    assert.equal(valid(s), false);
});
test("200-line cancellation proves every old accrual and preserves disabled historical asset", () => {
    const s = sample(true);
    s.input.action = s.receipt.action = "cancel";
    delete s.input.body.replacement;
    s.receipt.replacement = null;
    delete s.review.newAsset;
    s.review.oldAsset.enabled = false;
    assert.equal(valid(s), true);
    s.receipt.reversals.pop();
    assert.equal(valid(s), false);
});
test("positive-to-zero and removal still require full old reversals while zero-to-positive creates v1", () => {
    const s = sample(),
        body = s.input.body.replacement,
        r = s.receipt.replacement;
    body.lines.pop();
    r.lines.pop();
    body.lines[0].unit_price = r.lines[0].unit_price = "0.00";
    Object.assign(r.lines[0], { net_amount: "0", total_amount: "0", posting: null });
    r.net_amount = r.total_amount = "2";
    assert.equal(valid(s), true);
    assert.equal(r.lines[1].posting.operation_version, 1);
    s.receipt.reversals.pop();
    assert.equal(valid(s), false);
});
test("BTC and ETH replacements retain exact smallest units across all three accruals", () => {
    for (const [code, scale, network, unit, total] of [
        ["BTC", 8, "bitcoin", "0.00000001", "0.00000003"],
        ["ETH", 18, "ethereum", "0.000000000000000001", "0.000000000000000003"],
    ]) {
        const s = sample(),
            b = s.input.body.replacement,
            r = s.receipt.replacement;
        Object.assign(s.review.newAsset, { asset_id: code, code, kind: "native", network, scale });
        b.asset_id = r.asset_id = code;
        b.lines.forEach((row) => {
            row.unit_price = unit;
        });
        r.lines.forEach((row) => {
            row.unit_price = row.net_amount = row.total_amount = unit;
        });
        r.net_amount = r.total_amount = total;
        assert.equal(valid(s), true);
        r.lines[2].total_amount = "0";
        assert.equal(valid(s), false);
    }
});
test("stored 18-place exact quantities are accepted without rewriting the frozen receipt", () => {
    const s = sample();
    for (const group of [s.receipt.previous, s.receipt.replacement])
        for (const fact of [group, ...group.lines])
            for (const k of ["net_amount", "tax_amount", "total_amount"]) {
                fact[k] = fact[k].split(".")[0] + "." + "0".repeat(18);
            }
    const before = JSON.stringify(s.receipt);
    assert.equal(valid(s), true);
    assert.equal(JSON.stringify(s.receipt), before);
});
test("invoice and bill facts stay distinct and cannot prove another pending command", () => {
    const s = sample();
    s.review.current.document_kind =
        s.review.current.document.document_kind =
        s.input.body.replacement.document_kind =
        s.receipt.previous.document_kind =
        s.receipt.replacement.document_kind =
            "invoice";
    s.review.current.document.lines.forEach((r) => {
        r.category_kind = "income";
    });
    assert.equal(valid(s), true);
    const frozen = plan(s);
    assert.equal(validDocumentRevisionReceipt(s.review.current, frozen), false);
    assert.equal(validDocumentRevisionReceipt(s.receipt, { ...frozen, ledgerId: id(8000) }), false);
    assert.equal(
        validDocumentRevisionReceipt(s.receipt, { ...frozen, documentId: id(8000) }),
        false,
    );
    assert.equal(validDocumentRevisionReceipt(s.receipt, { ...frozen, bodyJson: "{" }), false);
});
const faults = {
    "wrong kind": (r) => {
        r.kind = "business_confirmation";
    },
    "wrong ledger": (r) => {
        r.ledger_id = id(8000);
    },
    "wrong document": (r) => {
        r.document_id = id(8000);
    },
    "wrong confirmation": (r) => {
        r.confirmation_id = id(8000);
    },
    "same old revision": (r, s) => {
        r.revision_id = s.review.current.revision_id;
    },
    "non UUID revision": (r) => {
        r.revision_id = "x";
    },
    "wrong previous version": (r) => {
        r.previous_version -= 1;
    },
    "skipped version": (r) => {
        r.version += 1;
    },
    "wrong action": (r) => {
        r.action = "cancel";
    },
    "untrimmed reason": (r) => {
        r.reason += " ";
    },
    "missing old group": (r) => {
        r.previous = null;
    },
    "missing new group": (r) => {
        r.replacement = null;
    },
    "wrong old asset": (r) => {
        r.previous.asset_id = "EUR";
    },
    "wrong new asset": (r) => {
        r.replacement.asset_id = "USD";
    },
    "wrong old party": (r) => {
        r.previous.party_id = id(8000);
    },
    "wrong new issue date": (r) => {
        r.replacement.issue_date = "2026-01-01";
    },
    "extra top field": (r) => {
        r.owner = "another";
    },
    "extra group field": (r) => {
        r.previous.document_version = 9;
    },
    "old zero omitted": (r) => {
        r.previous.lines.splice(1, 1);
    },
    "old sparse position changed": (r) => {
        r.previous.lines[0].line_no = 1;
    },
    "new position changed": (r) => {
        r.replacement.lines[0].line_no = 2;
    },
    "old order changed": (r) => {
        r.previous.lines.reverse();
    },
    "new order changed": (r) => {
        r.replacement.lines.reverse();
    },
    "old snapshot price changed": (r) => {
        r.previous.lines[0].unit_price = "2";
    },
    "raw source price reformatted": (r) => {
        r.replacement.lines[0].unit_price = "2";
    },
    "raw quantity reformatted": (r) => {
        r.replacement.lines[0].quantity = "1.0";
    },
    "wrong line dimension": (r) => {
        r.previous.lines[0].project_id = null;
    },
    "wrong line date": (r) => {
        r.replacement.lines[0].recognition_date = "2026-01-01";
    },
    "wrong prior operation": (r) => {
        r.previous.lines[0].posting.operation_id = id(8000);
    },
    "wrong prior version": (r) => {
        r.previous.lines[0].posting.operation_version += 1;
    },
    "wrong prior journal": (r) => {
        r.previous.lines[0].posting.journal_id = id(8000);
    },
    "old zero acquires posting": (r) => {
        r.previous.lines[1].posting = r.previous.lines[0].posting;
    },
    "retained operation replaced": (r) => {
        r.replacement.lines[0].posting.operation_id = id(8000);
    },
    "retained version unchanged": (r) => {
        r.replacement.lines[0].posting.operation_version -= 1;
    },
    "new operation begins at v2": (r) => {
        r.replacement.lines[1].posting.operation_version = 2;
    },
    "new operation reuses old": (r) => {
        r.replacement.lines[1].posting.operation_id = r.previous.lines[0].posting.operation_id;
    },
    "duplicate new operation": (r) => {
        r.replacement.lines[2].posting.operation_id = r.replacement.lines[0].posting.operation_id;
    },
    "duplicate replacement journal": (r) => {
        r.replacement.lines[2].posting.journal_id = r.replacement.lines[0].posting.journal_id;
    },
    "reversal reuses old journal": (r) => {
        r.reversals[0].journal_id = r.previous.lines[0].posting.journal_id;
    },
    "replacement reuses reversal": (r) => {
        r.replacement.lines[0].posting.journal_id = r.reversals[0].journal_id;
    },
    "reversal order changed": (r) => {
        r.reversals.reverse();
    },
    "reversal missing": (r) => {
        r.reversals.pop();
    },
    "reversal extra": (r) => {
        r.reversals.push(r.reversals[0]);
    },
    "reversal belongs to zero": (r) => {
        r.reversals[0].source_line_id = r.previous.lines[1].id;
    },
    "missing positive posting": (r) => {
        r.replacement.lines[0].posting = null;
    },
    "numeric amount": (r) => {
        r.replacement.lines[0].total_amount = 2;
    },
    "line total changed": (r) => {
        r.replacement.lines[0].total_amount = "2.01";
    },
    "header total changed": (r) => {
        r.previous.total_amount = "3";
    },
    "negative zero": (r) => {
        r.previous.tax_amount = "-0";
    },
};
for (const [name, mutate] of Object.entries(faults))
    test(`malformed fixed receipt remains unknown: ${name}`, () => {
        const s = sample();
        mutate(s.receipt, s);
        assert.equal(valid(s), false);
    });
