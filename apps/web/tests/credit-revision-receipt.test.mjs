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
const { validCreditRevisionReview } = await import("../src/credit-revision-intent.ts");
const { validCreditRevisionReceipt } = await import("../src/credit-revision-receipt.ts");
const { freezeCreditValue } = await import("../src/credit-intent.ts");
import {
    fixture as reviewFixture,
    plan,
    ledger,
    creditId,
    documentId,
    id,
} from "./credit-revision-fixture.mjs";
const valid = (f) => validCreditRevisionReview(ledger, creditId, f.action, f.body, f.review);
function fixture() {
    const result = {
        kind: "business_credit_revision",
        credit_id: creditId,
        revision_id: id(8),
        ledger_id: ledger,
        document_id: documentId,
        asset_id: "USD",
        previous_version: 3,
        version: 4,
        action: "correct",
        reason: "Fictional correction 中文",
        previous_credit_date: "2026-11-05",
        previous_credit_reason: "Fictional current",
        credit_date: "2026-11-06",
        replacement_reason: "Fictional replacement",
        members: [
            {
                action: "create",
                source_line_id: id(103),
                source_operation_id: id(203),
                operation_id: id(403),
                operation_version: 1,
                previous_line_no: null,
                previous_operation_version: null,
                previous_journal_id: null,
                reversal_journal_id: null,
                previous_net_amount: null,
                previous_tax_amount: null,
                previous_total_amount: null,
                previous_recognition_date: null,
                line_no: 1,
                journal_id: id(603),
                net_amount: "0.000000000000000000",
                tax_amount: "5.000000000000000000",
                total_amount: "5.000000000000000000",
                recognition_date: "2026-11-04",
            },
            {
                action: "correct",
                source_line_id: id(101),
                source_operation_id: id(201),
                operation_id: id(401),
                operation_version: 3,
                previous_line_no: 1,
                previous_operation_version: 2,
                previous_journal_id: id(501),
                reversal_journal_id: id(701),
                previous_net_amount: "17.230000000000000000",
                previous_tax_amount: "2.770000000000000000",
                previous_total_amount: "20.000000000000000000",
                previous_recognition_date: "2026-11-01",
                line_no: 2,
                journal_id: id(601),
                net_amount: "20.000000000000000000",
                tax_amount: "3.000000000000000000",
                total_amount: "23.000000000000000000",
                recognition_date: "2026-11-03",
            },
            {
                action: "cancel",
                source_line_id: id(102),
                source_operation_id: id(202),
                operation_id: id(402),
                operation_version: 2,
                previous_line_no: 2,
                previous_operation_version: 1,
                previous_journal_id: id(502),
                reversal_journal_id: id(702),
                previous_net_amount: "10.000000000000000000",
                previous_tax_amount: "0.000000000000000000",
                previous_total_amount: "10.000000000000000000",
                previous_recognition_date: "2026-11-01",
                line_no: null,
                journal_id: null,
                net_amount: null,
                tax_amount: null,
                total_amount: null,
                recognition_date: null,
            },
        ],
    };
    return { ...reviewFixture(), result };
}
function cancelled() {
    const f = fixture();
    f.action = f.result.action = "cancel";
    delete f.body.replacement;
    delete f.review.capacity;
    f.review.asset.enabled = false;
    f.result.credit_date = f.result.replacement_reason = null;
    const retained = f.result.members[1];
    retained.action = "cancel";
    for (const key of [
        "line_no",
        "journal_id",
        "net_amount",
        "tax_amount",
        "total_amount",
        "recognition_date",
    ])
        retained[key] = null;
    f.result.members = [retained, f.result.members[2]];
    return f;
}
test("fictional complete C/A replacement proves create, retained and removed facts independently", () => {
    for (const f of [fixture(), cancelled()]) {
        const original = JSON.stringify(f),
            frozen = plan(f);
        assert(valid(f));
        assert(validCreditRevisionReceipt(f.result, frozen));
        assert.equal(JSON.stringify(f), original);
        const saved = freezeCreditValue(f.result);
        assert(
            Object.isFrozen(saved) &&
                Object.isFrozen(saved.members) &&
                saved.members.every(Object.isFrozen),
        );
        f.result.members[0].operation_version = 999;
        assert(validCreditRevisionReceipt(saved, frozen));
        assert.equal(frozen.bodyJson, JSON.stringify(f.body));
    }
});
test("receipt rejects every changed group field, unknown field and incomplete member sequence", () => {
    const cases = {
        kind: "business_credit",
        credit_id: id(90),
        revision_id: creditId,
        ledger_id: id(90),
        document_id: id(90),
        asset_id: "EUR",
        previous_version: 2,
        version: 5,
        action: "cancel",
        reason: "Fictional different",
        previous_credit_date: "2026-11-04",
        previous_credit_reason: "Fictional different",
        credit_date: "2026-11-07",
        replacement_reason: "Fictional different",
        extra: true,
    };
    for (const [key, value] of Object.entries(cases)) {
        const f = fixture();
        f.result[key] = value;
        assert(!validCreditRevisionReceipt(f.result, plan(f)), key);
    }
    for (const mutate of [
        (f) => f.result.members.pop(),
        (f) => f.result.members.push(f.result.members[0]),
        (f) => f.result.members.reverse(),
        (f) => (f.result.revision_id = f.review.current.revision_id),
    ]) {
        const f = fixture();
        mutate(f);
        assert(!validCreditRevisionReceipt(f.result, plan(f)));
    }
});
test("receipt checks every previous/current field, null group, operation and global journal identity", () => {
    const mutations = {
        action: "create",
        source_line_id: id(102),
        source_operation_id: id(202),
        operation_id: id(99),
        operation_version: 4,
        previous_line_no: 2,
        previous_operation_version: 3,
        previous_journal_id: id(99),
        reversal_journal_id: null,
        previous_net_amount: "17.24",
        previous_tax_amount: "2.76",
        previous_total_amount: "20.01",
        previous_recognition_date: "2026-11-02",
        line_no: 1,
        journal_id: null,
        net_amount: "20.01",
        tax_amount: "2.99",
        total_amount: "23.01",
        recognition_date: "2026-11-04",
        extra: true,
    };
    for (const [key, value] of Object.entries(mutations)) {
        const f = fixture();
        f.result.members[1][key] = value;
        assert(!validCreditRevisionReceipt(f.result, plan(f)), key);
    }
    for (const field of [
        "previous_line_no",
        "previous_operation_version",
        "previous_journal_id",
        "reversal_journal_id",
        "previous_net_amount",
        "previous_tax_amount",
        "previous_total_amount",
        "previous_recognition_date",
    ]) {
        const f = fixture();
        f.result.members[0][field] = f.result.members[1][field];
        assert(!validCreditRevisionReceipt(f.result, plan(f)), field);
    }
    for (const field of [
        "line_no",
        "journal_id",
        "net_amount",
        "tax_amount",
        "total_amount",
        "recognition_date",
    ]) {
        const f = cancelled();
        f.result.members[0][field] = fixture().result.members[1][field];
        assert(!validCreditRevisionReceipt(f.result, plan(f)), field);
    }
    for (const mutate of [
        (f) => (f.result.members[0].operation_id = id(402)),
        (f) => (f.result.members[0].operation_id = id(202)),
        (f) => (f.result.members[0].operation_version = 4),
        (f) => (f.result.members[0].journal_id = id(502)),
        (f) => (f.result.members[0].journal_id = id(701)),
        (f) => (f.result.members[0].journal_id = id(303)),
        (f) => (f.result.members[0].net_amount = "0.000000000000000001"),
    ]) {
        const f = fixture();
        mutate(f);
        assert(!validCreditRevisionReceipt(f.result, plan(f)));
    }
});
test("frozen path, action and review remain proof inputs; current GET is never a receipt", () => {
    const f = fixture(),
        frozen = plan(f);
    for (const patch of [
        { ledgerId: id(90) },
        { creditId: id(90) },
        { action: "cancel" },
        { action: "unknown" },
        { bodyJson: "{" },
        { reviewedJson: "null" },
    ])
        assert(!validCreditRevisionReceipt(f.result, { ...frozen, ...patch }));
    assert(!validCreditRevisionReceipt(f.review.current, frozen));
    assert(!validCreditRevisionReceipt({ ...f.result, kind: "business_credit" }, frozen));
});
test("first version and final integer group version advance operation versions independently", () => {
    for (const version of [1, 2147483646]) {
        const f = cancelled();
        f.body.expected_version = f.review.current.version = f.result.previous_version = version;
        f.result.version = version + 1;
        if (version === 1) f.review.current.revision_id = f.review.current.revision_reason = null;
        f.review.current.members[0].operation_version = version;
        f.result.members[0].previous_operation_version = version;
        f.result.members[0].operation_version = version + 1;
        assert(valid(f));
        assert(validCreditRevisionReceipt(f.result, plan(f)));
    }
});

test("saved Unicode reasons use Python stripping and codepoint lengths, never JS trim", () => {
    for (const [raw, saved] of [
        [" \u0085Fictional\u001f ", "Fictional"],
        ["\ufeffFictional\ufeff", "\ufeffFictional\ufeff"],
        [" " + "😀".repeat(1000) + " ", "😀".repeat(1000)],
    ]) {
        const f = fixture();
        f.body.reason = f.body.replacement.reason = raw;
        f.result.reason = f.result.replacement_reason = saved;
        f.review.current.credit_reason = f.result.previous_credit_reason = "😀".repeat(1001);
        const frozen = plan(f);
        assert(valid(f));
        assert(validCreditRevisionReceipt(f.result, frozen));
        for (const field of ["reason", "replacement_reason"]) {
            f.result[field] = saved === raw.trim() ? saved + "x" : raw.trim();
            assert(!validCreditRevisionReceipt(f.result, frozen), field);
            f.result[field] = saved;
        }
        assert.equal(JSON.parse(frozen.bodyJson).reason, raw);
    }
});

test("all old and new receipt quantities reject signed zero and non-decimal shapes", () => {
    for (const [index, field] of [
        [0, "net_amount"],
        [2, "previous_tax_amount"],
    ]) {
        const f = fixture(),
            frozen = plan(f);
        for (const value of [
            "-0",
            "-0.000000000000000000",
            "0e0",
            0,
            "00.0",
            "0.0000000000000000000",
        ]) {
            f.result.members[index][field] = value;
            assert(!validCreditRevisionReceipt(f.result, frozen), `${field}: ${value}`);
        }
        f.result.members[index][field] = "0.000000000000000000";
        assert(validCreditRevisionReceipt(f.result, frozen));
    }
    for (const mutate of [
        (f) => (f.review.current.status = "cancelled"),
        (f) => (f.review.current.version = 4),
        (f) => (f.review.asset.asset_id = "EUR"),
        (f) => (f.review.current.members[0].total_amount = "20.01"),
    ]) {
        const f = fixture();
        mutate(f);
        assert(!validCreditRevisionReceipt(f.result, plan(f)));
    }
});

function largeFixture(swap) {
    const f = fixture(),
        numbers = Array.from({ length: 200 }, (_, i) => i + 1);
    const before = swap ? numbers.slice(0, 100) : numbers;
    const after = swap ? numbers.slice(100) : numbers;
    f.review.confirmation.postings = numbers.map((n) => ({
        line_id: id(1000 + n),
        line_no: n,
        operation_id: id(2000 + n),
        journal_id: id(3000 + n),
    }));
    const capacity = f.review.capacity.lines[0];
    f.review.capacity.lines = numbers.map((n) => ({
        ...capacity,
        source_line_id: id(1000 + n),
        net_amount: "1.00",
        tax_amount: "0.00",
        total_amount: "1.00",
        credited_net_amount: before.includes(n) ? "1.00" : "0.00",
        credited_tax_amount: "0.00",
        applied_amount: "0.00",
        remaining_net_amount: before.includes(n) ? "0.00" : "1.00",
        remaining_tax_amount: "0.00",
        available_gross_amount: before.includes(n) ? "0.00" : "1.00",
    }));
    f.review.current.members = before.map((n, i) => ({
        line_no: i + 1,
        source_line_id: id(1000 + n),
        source_operation_id: id(2000 + n),
        operation_id: id(4000 + n),
        operation_version: 1,
        journal_id: id(5000 + n),
        net_amount: "1.00",
        tax_amount: "0.00",
        total_amount: "1.00",
        recognition_date: "2026-11-01",
    }));
    f.body.replacement.lines = after.map((n) => ({
        source_line_id: id(1000 + n),
        net_amount: "1.00",
        tax_amount: "0.00",
        recognition_date: "2026-11-03",
    }));
    const templates = f.result.members;
    function expected(action, n, position) {
        const row = {
            ...templates[{ create: 0, correct: 1, cancel: 2 }[action]],
            action,
            source_line_id: id(1000 + n),
            source_operation_id: id(2000 + n),
            operation_id: id((action === "create" ? 10000 : 4000) + n),
            operation_version: action === "create" ? 1 : 2,
        };
        if (action !== "create")
            Object.assign(row, {
                previous_line_no: n,
                previous_operation_version: 1,
                previous_journal_id: id(5000 + n),
                reversal_journal_id: id(7000 + n),
                previous_net_amount: "1.000000000000000000",
                previous_tax_amount: "0.000000000000000000",
                previous_total_amount: "1.000000000000000000",
            });
        if (action !== "cancel")
            Object.assign(row, {
                line_no: position,
                journal_id: id(6000 + n),
                net_amount: "1.000000000000000000",
                tax_amount: "0.000000000000000000",
                total_amount: "1.000000000000000000",
                recognition_date: "2026-11-03",
            });
        return row;
    }
    f.result.members = swap
        ? [
              ...after.map((n, i) => expected("create", n, i + 1)),
              ...before.map((n) => expected("cancel", n, null)),
          ]
        : after.map((n, i) => expected("correct", n, i + 1));
    return f;
}

test("200 unchanged retained members still reverse; 100 swap into 100 other sources without 400 source fiction", () => {
    for (const swap of [false, true]) {
        const f = largeFixture(swap),
            frozen = plan(f);
        assert.equal(f.review.confirmation.postings.length, 200);
        assert.equal(f.result.members.length, 200);
        assert(valid(f));
        assert(validCreditRevisionReceipt(f.result, frozen));
        const last = f.result.members[199];
        last.previous_journal_id = f.result.members[0].journal_id;
        assert(!validCreditRevisionReceipt(f.result, frozen));
        f.result.members.pop();
        assert(!validCreditRevisionReceipt(f.result, frozen));
    }
    const f = largeFixture(false);
    f.body.replacement.lines.push({ ...f.body.replacement.lines[0], source_line_id: id(99999) });
    assert(!validCreditRevisionReceipt(f.result, plan(f)));
});

test("ETH 18-place minimum and NUMERIC maximum prove exact amounts without changing frozen spelling", () => {
    const minimum = "0.000000000000000001",
        maximum = "99999999999999999999.999999999999999999";
    for (const amount of [minimum, maximum]) {
        const f = fixture(),
            old = f.review.current.members[0],
            row = f.review.capacity.lines[0],
            member = f.result.members[1];
        f.review.asset = {
            ...f.review.asset,
            asset_id: "ETH",
            code: "ETH",
            kind: "native",
            scale: 18,
            network: "ethereum",
        };
        f.review.current.asset_id = f.review.capacity.asset_id = f.result.asset_id = "ETH";
        f.review.confirmation.postings = [f.review.confirmation.postings[0]];
        f.review.current.members = [
            { ...old, net_amount: minimum, tax_amount: "0", total_amount: minimum },
        ];
        f.review.capacity.lines = [
            {
                ...row,
                net_amount: maximum,
                tax_amount: "0",
                total_amount: maximum,
                credited_net_amount: minimum,
                credited_tax_amount: "0",
                applied_amount: "0",
                remaining_net_amount: "99999999999999999999.999999999999999998",
                remaining_tax_amount: "0",
                available_gross_amount: "99999999999999999999.999999999999999998",
            },
        ];
        f.body.replacement.lines = [
            { ...f.body.replacement.lines[1], net_amount: amount, tax_amount: "0" },
        ];
        f.result.members = [
            {
                ...member,
                line_no: 1,
                previous_net_amount: minimum,
                previous_tax_amount: "0",
                previous_total_amount: minimum,
                net_amount: amount,
                tax_amount: "0.000000000000000000",
                total_amount: amount,
            },
        ];
        const frozen = plan(f);
        assert(valid(f));
        assert(validCreditRevisionReceipt(f.result, frozen));
        assert.equal(freezeCreditValue(f.result).members[0].net_amount, amount);
        for (const invalid of ["100000000000000000000", "0.0000000000000000001"]) {
            f.result.members[0].net_amount = invalid;
            assert(!validCreditRevisionReceipt(f.result, frozen));
        }
        assert.equal(JSON.parse(frozen.bodyJson).replacement.lines[0].net_amount, amount);
    }
});
