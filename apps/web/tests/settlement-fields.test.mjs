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
const { paymentFieldError, allocationFieldError } = await import("../src/settlement-fields.ts");
const id = (n) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const ledger = id(1);
const assets = [
    { asset_id: "USD", enabled: true, scale: 2 },
    { asset_id: "EUR", enabled: true, scale: 2 },
    { asset_id: "BTC", enabled: true, scale: 8 },
];
const refs = {
    assets,
    parties: [
        {
            id: id(2),
            ledger_id: ledger,
            name: "Fictional party",
            archived: false,
            role: "both",
        },
    ],
    accounts: [
        {
            id: id(3),
            ledger_id: ledger,
            archived: false,
            asset_ids: ["USD", "BTC"],
        },
    ],
    categories: [{ id: id(4), ledger_id: ledger, archived: false, kind: "expense" }],
};
function payment() {
    return {
        id: id(5),
        direction: "payout",
        party_id: id(2),
        account_id: id(3),
        asset_id: "USD",
        amount: "110.00",
        transaction_date: "2026-09-15",
        fees: [
            {
                account_id: id(3),
                asset_id: "BTC",
                amount: "0.00000001",
                category_id: id(4),
            },
        ],
    };
}
function allocation(asset = "EUR") {
    return {
        payment: {
            ledger_id: ledger,
            party_id: id(2),
            direction: "payout",
            asset_id: "USD",
            transaction_date: "2026-09-15",
            unapplied_amount: "110.00",
        },
        group: {
            id: id(6),
            allocation_date: "2026-09-30",
            lines: [
                {
                    document_id: id(7),
                    document_line_id: id(8),
                    payment_amount: "110.00",
                    document_amount: asset === "USD" ? "110.00" : "100.00",
                },
            ],
        },
        sources: [
            {
                ledger_id: ledger,
                party_id: id(2),
                document_kind: "bill",
                document_id: id(7),
                document_line_id: id(8),
                issue_date: "2026-09-30",
                asset_id: asset,
                outstanding_amount: asset === "USD" ? "110.00" : "100.00",
                description: "Fictional source",
                project_id: id(9),
            },
        ],
    };
}
const check = (value) => allocationFieldError(value.group, value.payment, value.sources, assets);

test("new payment preserves amounts and validates independent fee assets and active owned references", () => {
    const body = payment(),
        original = structuredClone(body);
    assert.equal(paymentFieldError(ledger, body, refs), null);
    assert.deepEqual(body, original);
    for (const mutate of [
        (r) => {
            r.parties[0].archived = true;
        },
        (r) => {
            r.parties[0].role = "customer";
        },
        (r) => {
            r.parties[0].ledger_id = id(999);
        },
        (r) => {
            r.accounts[0].archived = true;
        },
        (r) => {
            r.accounts[0].asset_ids = ["USD"];
        },
        (r) => {
            r.categories[0].kind = "income";
        },
        (r) => {
            r.categories[0].ledger_id = id(999);
        },
        (r) => {
            r.assets[2].enabled = false;
        },
    ]) {
        const changed = structuredClone(refs);
        mutate(changed);
        assert.equal(paymentFieldError(ledger, body, changed), "reference");
    }
    body.fees[0].amount = "0.000000001";
    assert.equal(paymentFieldError(ledger, body, refs), "amount");
    body.transaction_date = "2026-02-30";
    assert.equal(paymentFieldError(ledger, body, refs), "date");
});
test("backdating accepts the exact lower bound and rejects either earlier source date", () => {
    const value = allocation();
    assert.equal(check(value), null);
    value.group.allocation_date = "2026-09-29";
    assert.equal(check(value), "date");
    value.sources[0].issue_date = "2026-09-01";
    value.group.allocation_date = "2026-09-14";
    assert.equal(check(value), "date");
    value.group.allocation_date = "2026-09-15";
    assert.equal(check(value), null);
});
test("same asset must match, cross assets stay explicit, and capacity uses exact integer arithmetic", () => {
    const same = allocation("USD");
    assert.equal(check(same), null);
    same.group.lines[0].document_amount = "109.99";
    assert.equal(check(same), "amount");
    const cross = allocation();
    assert.equal(check(cross), null);
    cross.group.lines[0].document_amount = "100.01";
    assert.equal(check(cross), "capacity");
    cross.group.lines[0].document_amount = "99.00";
    cross.group.lines[0].payment_amount = "110.01";
    assert.equal(check(cross), "capacity");
    cross.group.lines[0].payment_amount = "0.001";
    assert.equal(check(cross), "amount");
});
test("sealed identity, direction, scope and all selected source rows must match", () => {
    for (const [field, replacement] of [
        ["ledger_id", id(999)],
        ["party_id", id(999)],
        ["document_id", id(999)],
        ["document_line_id", id(999)],
        ["document_kind", "invoice"],
    ]) {
        const value = allocation();
        value.sources[0][field] = replacement;
        assert.equal(check(value), "source");
    }
    const value = allocation();
    value.sources = [];
    assert.equal(check(value), "source");
    const duplicate = allocation();
    duplicate.group.lines.push({ ...duplicate.group.lines[0] });
    duplicate.sources.push({ ...duplicate.sources[0] });
    assert.equal(check(duplicate), "source");
});
test("multiple lines sum only payment-side asset and retain historical dimensions without active masters", () => {
    const value = allocation();
    value.group.lines[0].payment_amount = "60.00";
    value.group.lines[0].document_amount = "50.00";
    value.group.lines.push({
        document_id: id(10),
        document_line_id: id(11),
        payment_amount: "50.00",
        document_amount: "0.00000001",
    });
    value.sources.push({
        ...value.sources[0],
        document_id: id(10),
        document_line_id: id(11),
        asset_id: "BTC",
        outstanding_amount: "0.00000001",
        project_id: id(999),
    });
    const original = structuredClone(value);
    assert.equal(check(value), null);
    assert.deepEqual(value, original);
    value.group.lines[1].payment_amount = "50.01";
    assert.equal(check(value), "capacity");
});

test("200 one-cent rows stay exact and the 201st row is rejected", () => {
    const value = allocation("USD");
    const source = value.sources[0];
    value.payment.unapplied_amount = "2.00";
    value.group.lines = Array.from({ length: 200 }, (_, n) => ({
        document_id: id(7),
        document_line_id: id(100 + n),
        payment_amount: "0.01",
        document_amount: "0.01",
    }));
    value.sources = value.group.lines.map((line) => ({
        ...source,
        document_line_id: line.document_line_id,
        outstanding_amount: "0.01",
    }));
    assert.equal(check(value), null);
    value.payment.unapplied_amount = "1.99";
    assert.equal(check(value), "capacity");
    value.group.lines.push({
        ...value.group.lines[0],
        document_line_id: id(999),
    });
    value.sources.push({ ...value.sources[0], document_line_id: id(999) });
    assert.equal(check(value), "source");
});

test("historical allocations still require enabled actual assets and bounded positive quantities", () => {
    const value = allocation();
    for (const disabled of ["USD", "EUR"])
        assert.equal(
            allocationFieldError(
                value.group,
                value.payment,
                value.sources,
                assets.map((asset) => ({ ...asset, enabled: asset.asset_id !== disabled })),
            ),
            "reference",
        );
    for (const amount of ["0", "-1.00", "100000000000000000000.00", "1e2", "1,00"]) {
        value.group.lines[0].document_amount = amount;
        assert.equal(check(value), "amount");
    }
});
