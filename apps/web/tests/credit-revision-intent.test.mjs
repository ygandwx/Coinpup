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
const { validCreditRevisionReview, stripCreditRevisionReason } =
    await import("../src/credit-revision-intent.ts");
import { fixture, cancelled, ledger, creditId, id } from "./credit-revision-fixture.mjs";
const valid = (f) => validCreditRevisionReview(ledger, creditId, f.action, f.body, f.review);
test("pure review preserves input spelling and accepts disabled-reference cancellation", () => {
    for (const f of [fixture(), cancelled()]) {
        const original = JSON.stringify(f);
        assert(valid(f));
        assert.equal(JSON.stringify(f), original);
        assert.equal(f.body.reason, "  Fictional correction 中文  ");
        assert.equal(f.review.current.members[0].net_amount, "17.23");
    }
});
test("capacity excludes only this current group and separately enforces net, tax and gross", () => {
    const f = fixture(),
        line = f.body.replacement.lines[1];
    line.net_amount = "47.23";
    line.tax_amount = "8.77"; // exactly 56: old available 36 plus this group's 20
    assert(valid(f));
    line.net_amount = "47.24";
    assert(!valid(f));
    line.net_amount = "1";
    line.tax_amount = "8.78";
    assert(!valid(f));
    line.tax_amount = "0";
    line.net_amount = "97.24";
    f.review.capacity.lines[0].applied_amount = "0.00";
    f.review.capacity.lines[0].available_gross_amount = "86.00";
    assert(!valid(f)); // net max97.23 even though gross would fit
    line.net_amount = "97.23";
    assert(valid(f));
});
test("review rejects malformed, foreign, incomplete and inconsistent new-intent snapshots", () => {
    const cases = {
        groupVersion: (f) => (f.body.expected_version = 2),
        missingReplacement: (f) => delete f.body.replacement,
        emptyReplacement: (f) => (f.body.replacement.lines = []),
        extraInput: (f) => (f.body.id = id(99)),
        extraReplacement: (f) => (f.body.replacement.id = id(99)),
        duplicateSource: (f) => (f.body.replacement.lines[1].source_line_id = id(103)),
        float: (f) => (f.body.replacement.lines[0].tax_amount = 5),
        precision: (f) => (f.body.replacement.lines[0].tax_amount = "5.000"),
        exponent: (f) => (f.body.replacement.lines[0].tax_amount = "5e0"),
        negativeZeroNet: (f) => (f.body.replacement.lines[0].net_amount = "-0"),
        negativeZeroTax: (f) => (f.body.replacement.lines[1].tax_amount = "-0.00"),
        zero: (f) => (f.body.replacement.lines[0].tax_amount = "0"),
        earlyDate: (f) => (f.body.replacement.credit_date = "2026-09-30"),
        invalidDate: (f) => (f.body.replacement.lines[0].recognition_date = "2026-02-30"),
        reason: (f) => (f.body.reason = " \0 "),
        reasonLimit: (f) => (f.body.reason = "x".repeat(1001)),
        wrongLedger: (f) => (f.review.current.ledger_id = id(90)),
        wrongCredit: (f) => (f.review.current.credit_id = id(90)),
        wrongDocument: (f) => (f.review.current.document_id = id(90)),
        asset: (f) => (f.review.asset.asset_id = "EUR"),
        assetMetadata: (f) => (f.review.asset.network = "fictional-other-network"),
        disabled: (f) => (f.review.asset.enabled = false),
        overflow: (f) => (f.review.current.version = f.body.expected_version = 2147483647),
        missingVersionFact: (f) => (f.review.current.revision_id = null),
        terminal: (f) => (f.review.current.status = "cancelled"),
        emptyCurrent: (f) => (f.review.current.members = []),
        currentOrder: (f) => f.review.current.members.reverse(),
        currentOperation: (f) => (f.review.current.members[1].operation_id = id(401)),
        sourceAsCredit: (f) => (f.review.current.members[0].operation_id = id(201)),
        sourceAsJournal: (f) => (f.review.current.members[0].journal_id = id(303)),
        futureOperation: (f) => (f.review.current.members[0].operation_version = 4),
        sourceMapping: (f) => (f.review.current.members[0].source_operation_id = id(202)),
        oldTotal: (f) => (f.review.current.members[0].total_amount = "20.01"),
        oldNegativeZero: (f) => (f.review.current.members[1].tax_amount = "-0.00"),
        confirmationVersion: (f) => (f.review.confirmation.document_version = 3),
        duplicateConfirmation: (f) => (f.review.confirmation.postings[1].line_id = id(101)),
        confirmationOrder: (f) => f.review.confirmation.postings.reverse(),
        missingCapacity: (f) => f.review.capacity.lines.pop(),
        duplicatedCapacity: (f) => (f.review.capacity.lines[2].source_line_id = id(101)),
        scopeCapacity: (f) => (f.review.capacity.party_id = id(90)),
        sourceVersion: (f) => (f.review.capacity.expected_version = 3),
        staleOwn: (f) => (f.review.capacity.lines[0].credited_net_amount = "16.00"),
        formula: (f) => (f.review.capacity.lines[2].available_gross_amount = "109.99"),
    };
    for (const [name, mutate] of Object.entries(cases)) {
        const f = fixture();
        mutate(f);
        assert(!valid(f), name);
    }
    const f = cancelled();
    f.body.replacement = fixture().body.replacement;
    assert(!valid(f));
});
test("first and final integer group versions retain independent operation versions", () => {
    for (const version of [1, 2147483646]) {
        const f = cancelled();
        f.body.expected_version = f.review.current.version = version;
        if (version === 1) f.review.current.revision_id = f.review.current.revision_reason = null;
        f.review.current.members[0].operation_version = version;
        assert(valid(f));
    }
    const f = fixture();
    for (const [scope, credit, action, body, review] of [
        [id(90), creditId, f.action, f.body, f.review],
        [ledger, id(90), f.action, f.body, f.review],
        [ledger, creditId, "unknown", f.body, f.review],
        [ledger, creditId, f.action, null, f.review],
        [ledger, creditId, f.action, f.body, null],
    ])
        assert(!validCreditRevisionReview(scope, credit, action, body, review));
});

test("revision reasons match server codepoint limits and Python stripping without rewriting raw input", () => {
    const f = fixture();
    f.review.current.credit_reason = "😀".repeat(1001);
    f.body.reason = " " + "😀".repeat(1000) + " ";
    f.body.replacement.reason = "😀".repeat(2000);
    const original = JSON.stringify(f);
    assert(valid(f));
    assert.equal(JSON.stringify(f), original);
    f.body.reason = "😀".repeat(1001);
    assert(!valid(f));
    f.body.reason = "Fictional revision";
    f.body.replacement.reason += " "; // Replacement length is checked before stripping.
    assert(!valid(f));
    f.body.replacement.reason = "Fictional fact";
    for (const [raw, saved] of [
        ["\u0085Fictional\u0085", "Fictional"],
        ["\u001cFictional\u001f", "Fictional"],
        ["\ufeffFictional\ufeff", "\ufeffFictional\ufeff"],
        ["\ufeff", "\ufeff"],
    ]) {
        f.body.reason = f.body.replacement.reason = f.review.current.credit_reason = raw;
        assert.equal(stripCreditRevisionReason(raw), saved);
        assert(valid(f));
    }
    f.body.reason = "\u0085".repeat(1001) + "Fictional";
    assert(valid(f));
    f.body.reason = "\u0085\u001f";
    assert(!valid(f));
});
