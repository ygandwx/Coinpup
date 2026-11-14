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
const {
    validRefundFact,
    validRefundCurrent,
    validRefundHistoryPage,
    validRefundRevisionDetail,
    storedRefundUnits,
} = await import("../src/refund-revision-read.ts");
const { getRefundCurrent, listRefundHistory, getRefundRevision, correctRefund, cancelRefund } =
    await import("../src/refund-api.ts");
const id = (n) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const scope = {
    ledger_id: id(1),
    refund_id: id(2),
    payment_id: id(3),
    operation_id: id(4),
    payment_asset_id: "USD",
};
const assets = [
    { asset_id: "USD", scale: 2, enabled: true },
    { asset_id: "BTC", scale: 8, enabled: false },
    { asset_id: "ETH", scale: 18, enabled: true },
];
const fee = {
    account_id: id(6),
    category_id: id(7),
    asset_id: "ETH",
    amount: "0.000000000000000001",
};
function fact() {
    return {
        account_id: id(5),
        asset_id: "BTC",
        amount: "0.000000010000000000",
        payment_asset_id: "USD",
        payment_amount: "10.010000000000000000",
        source_version: 1,
        transaction_date: "2032-02-29",
        recognition_date: "2032-02-29",
        reason: "Fictional exact cash",
        fees: [fee, fee],
        allocation_return_ids: [id(8), id(9)],
    };
}
const row = (v, action) => ({
    version: v,
    action,
    revision_id: v === 1 ? null : id(v + 10),
    reason: "Fictional adjustment",
    created_at: "2032-02-29T14:03:22+00:00",
});
const receipt = (action) => ({
    kind: "business_refund_revision",
    refund_id: scope.refund_id,
    ledger_id: scope.ledger_id,
    payment_id: scope.payment_id,
    operation_id: scope.operation_id,
    revision_id: id(12),
    previous_version: 1,
    version: 2,
    action,
    reason: "Fictional adjustment",
    previous_journal_id: id(15),
    reversal_journal_id: id(16),
    replacement_journal_id: action === "correct" ? id(17) : null,
    previous: fact(),
    replacement: action === "correct" ? fact() : null,
});
test("stored units use actual disabled historical scales and retain ETH and BTC extremes", () => {
    assert.equal(storedRefundUnits("0.000000010000000000", "BTC", assets), 1n);
    assert.equal(
        storedRefundUnits("9".repeat(20) + "." + "9".repeat(18), "ETH", assets),
        10n ** 38n - 1n,
    );
    for (const value of ["0", "0.000000001", NaN, "1e9", "9".repeat(21), false])
        assert.throws(() => storedRefundUnits(value, "BTC", assets));
    for (const invalid of [[], [...assets, assets[1]], [{ asset_id: "BTC", scale: -1 }]])
        assert.throws(() => storedRefundUnits("1", "BTC", invalid));
});
test("both exact dimensions, ordered independent fees and unique evidence are mandatory", () => {
    assert(validRefundFact(fact(), "USD", assets));
    assert(
        validRefundFact(
            {
                ...fact(),
                allocation_return_ids: Array.from({ length: 200 }, (_, i) => id(i + 1)),
                fees: Array(20).fill(fee),
            },
            "USD",
            assets,
        ),
    );
    for (const change of [
        { bad: true },
        { amount: null },
        { asset_id: "USD", amount: "1.00" },
        { source_version: true },
        { reason: "\u0085cash" },
        { reason: "x".repeat(2001) },
        { reason: "a\0b" },
        { fees: Array(21).fill(fee) },
        { allocation_return_ids: [id(1), id(1)] },
        { allocation_return_ids: Array.from({ length: 201 }, (_, i) => id(i + 1)) },
        { fees: [{ ...fee, amount: 1 }] },
        { recognition_date: "2032-02-28" },
        { transaction_date: "2031-02-29", recognition_date: "2031-02-29" },
    ])
        assert.equal(validRefundFact({ ...fact(), ...change }, "USD", assets), false);
});
test("current status, owner-scoped operation and cancellation have a complete stable shape", () => {
    const active = {
        ...scope,
        party_id: id(10),
        payment_direction: "receipt",
        version: 1,
        status: "active",
        revision_id: null,
        revision_reason: null,
        fact: fact(),
        journal_id: id(15),
    };
    const check = (c) => validRefundCurrent(c, scope, id(10), "receipt", assets);
    assert(check(active));
    const end = {
        ...active,
        version: 2,
        status: "cancelled",
        revision_id: id(12),
        revision_reason: "Fictional cancellation",
        fact: null,
        journal_id: null,
    };
    assert(check(end));
    for (const c of [
        { ...active, refund_id: id(30) },
        { ...active, operation_id: id(30) },
        { ...active, payment_asset_id: "BTC" },
        { ...active, party_id: id(11) },
        { ...active, payment_direction: "payout" },
        { ...active, version: 2 },
        { ...end, fact: fact() },
        { ...end, journal_id: id(15) },
        { ...end, version: 1 },
        { ...end, revision_reason: " " },
    ])
        assert.equal(check(c), false);
});
test("version directories reject gaps, duplicate identities and resurrection", () => {
    const rows = [row(1, "create"), row(2, "correct"), row(3, "cancel")];
    assert(validRefundHistoryPage(rows, 0));
    assert(validRefundHistoryPage(rows.slice(1), 1));
    assert(validRefundHistoryPage([], 3));
    assert(!validRefundHistoryPage([], 0));
    for (const invalid of [
        [rows[0], rows[2]],
        [...rows, row(4, "correct")],
        [rows[0], rows[1], { ...rows[2], revision_id: rows[1].revision_id }],
        [{ ...rows[0], created_at: "2031-02-29T12:30:00Z" }],
        [{ ...rows[0], revision_id: id(2) }],
        Array(26).fill(rows[0]),
    ])
        assert.equal(validRefundHistoryPage(invalid, 0), false);
});
test("revision details bind selected immutable version, cash facts and distinct journals", () => {
    for (const action of ["correct", "cancel"]) {
        const result = receipt(action),
            selected = row(2, action);
        assert(validRefundRevisionDetail(result, scope, selected, assets));
        for (const bad of [
            { refund_id: id(55) },
            { version: 3 },
            { previous_version: 2 },
            { reason: "other" },
            { revision_id: id(13) },
            { previous: null },
            { previous_journal_id: result.reversal_journal_id },
            { replacement_journal_id: result.previous_journal_id },
            { replacement: action === "cancel" ? fact() : null },
        ]) {
            assert(!validRefundRevisionDetail({ ...result, ...bad }, scope, selected, assets));
        }
    }
});
test("transports scope writes by exact path and key and pass read cancellation through", async (t) => {
    const calls = [];
    t.mock.method(globalThis, "fetch", async (path, init) => {
        calls.push({ path, init });
        return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
    });
    const signal = new AbortController().signal;
    const base = `/api/v1/ledgers/${scope.ledger_id}/business-refunds/${scope.refund_id}`;
    await getRefundCurrent(scope.ledger_id, scope.refund_id, signal);
    await listRefundHistory(scope.ledger_id, scope.refund_id, 25, signal);
    await getRefundRevision(scope.ledger_id, scope.refund_id, id(12), signal);
    const body = { expected_version: 1, reason: "Fictional command 中文" };
    for (const send of [correctRefund, cancelRefund])
        await send("fictional-csrf", scope.ledger_id, scope.refund_id, body, "fictional-key");
    assert.deepEqual(
        calls.map((c) => c.path),
        [
            "/current",
            "/history?limit=25&offset=25",
            "/revisions/" + id(12),
            "/corrections",
            "/cancellations",
        ].map((p) => base + p),
    );
    for (const { init } of calls.slice(0, 3)) assert.equal(init.cache, "no-store");
    for (const { init } of calls.slice(3)) {
        assert.equal(init.method, "POST");
        assert.equal(init.body, JSON.stringify(body));
        const headers = new Headers(init.headers);
        assert.equal(headers.get("Idempotency-Key"), "fictional-key");
        assert.equal(headers.get("X-CSRF-Token"), "fictional-csrf");
        assert.equal(init.credentials, "same-origin");
    }
});

test("aborting history, current or single-revision reads reaches the in-flight fetch", async (t) => {
    t.mock.method(
        globalThis,
        "fetch",
        (_path, init) =>
            new Promise((_resolve, reject) => {
                init.signal.addEventListener(
                    "abort",
                    () => reject(new DOMException("Cancelled", "AbortError")),
                    { once: true },
                );
            }),
    );
    for (const read of [
        (signal) => getRefundCurrent(scope.ledger_id, scope.refund_id, signal),
        (signal) => listRefundHistory(scope.ledger_id, scope.refund_id, 0, signal),
        (signal) => getRefundRevision(scope.ledger_id, scope.refund_id, id(12), signal),
    ]) {
        const ctl = new AbortController();
        const pending = read(ctl.signal);
        ctl.abort();
        await assert.rejects(pending, (error) => error.kind === "network");
    }
});
