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
const { validInvoiceState, validInvoicePage, validInvoiceSequence, validListedInvoice } =
    await import("../src/invoice-issuance-receipt.ts");
const { listInvoiceHistory, issueInvoice } = await import("../src/invoice-api.ts");
const id = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const ledger = id(1),
    policy = { prefix: "示例-", minimum_digits: 20 },
    seq = "9223372036854775807";
const cmd = {
    id: id(2),
    document_id: id(3),
    expected_document_version: 5,
    configuration_id: id(4),
    expected_configuration_version: 9,
    previous_issuance_id: id(5),
};
const pending = () => ({
    kind: "invoice_issuance",
    id: cmd.id,
    document_id: cmd.document_id,
    ledger_id: ledger,
    document_version: 5,
    configuration_id: cmd.configuration_id,
    configuration_version: 9,
    previous_issuance_id: cmd.previous_issuance_id,
    numbering: { ...policy },
    sequence: seq,
    number: `示例-0${seq}`,
    snapshot_sha256: "1".repeat(64),
    template_version: "commercial-invoice-v1",
    state: "pending",
    artifacts: [],
});
const pair = () =>
    ["zh", "en"].map((language, i) => ({
        language,
        file_id: id(10 + i),
        sha256: String(i + 2).repeat(64),
        byte_size: 2048 + i,
        media_type: "application/pdf",
    }));
const complete = () => ({ ...pending(), state: "complete", artifacts: pair() });
const check = (v, prior) => validInvoiceState(v, ledger, cmd, policy, prior);
test("pending is an empty reservation; completion binds the reviewed version, predecessor, stable number and original bilingual pair", () => {
    assert.ok(check(pending()));
    assert.ok(check(complete(), pending()));
    for (const mutate of [
        (r) => r.document_version--,
        (r) => r.configuration_version++,
        (r) => (r.id = id(19)),
        (r) => (r.ledger_id = id(20)),
        (r) => (r.previous_issuance_id = null),
        (r) => (r.document_id = id(40)),
        (r) => (r.numbering.minimum_digits = 1),
        (r) => (r.snapshot_sha256 = "a".repeat(64)),
        (r) => (r.template_version = "other"),
        (r) => r.artifacts.reverse(),
        (r) => r.artifacts.pop(),
        (r) => (r.artifacts[1].file_id = r.artifacts[0].file_id),
        (r) => (r.artifacts[1].sha256 = r.artifacts[0].sha256),
        (r) => (r.artifacts[1].sha256 = "g".repeat(64)),
        (r) => (r.artifacts[1].byte_size = 8388609),
        (r) => (r.artifacts[0].file_id += "\n"),
        (r) => (r.state = "pending"),
        (r) => (r.key = "leak"),
    ]) {
        const r = complete();
        mutate(r);
        assert.equal(check(r, pending()), false);
    }
    assert.equal(check(pending(), complete()), false);
    assert.equal(check({ ...pending(), number: "示例-" + seq }), false);
});
const row = (i, sequence = seq) => ({
    id: id(i),
    ledger_id: ledger,
    document_id: cmd.document_id,
    document_version: 5,
    current_document_version: 6,
    document_status: "superseded",
    previous_issuance_id: cmd.previous_issuance_id,
    sequence,
    number: "示例-" + sequence.padStart(20, "0"),
    state: "complete",
});
test("history cursor is exact across int64 and 200 items; ordering, ownership and page continuation bind to the request", () => {
    for (const n of ["", 0, "01", "0", "1.0", "1\n", "-3", "9223372036854775808"])
        assert.equal(validInvoiceSequence(n), false);
    assert.ok(validInvoiceSequence(seq));
    const items = Array.from({ length: 200 }, (_, i) =>
        row(30 + i, String(BigInt(seq) - BigInt(i))),
    );
    const page = { items, next_before: items.at(-1).sequence };
    const valid = (v) => validInvoicePage(v, ledger, cmd.document_id, null, 200);
    assert.ok(valid(page));
    assert.ok(valid({ ...page, next_before: null }));
    for (const mutate of [
        (p) => p.items.push(row(901, "1")),
        (p) => (p.items[3].sequence = p.items[2].sequence),
        (p) => (p.items[3].id = p.items[2].id),
        (p) => (p.items[3].document_id = id(50)),
        (p) => (p.items[3].ledger_id = id(99)),
        (p) => (p.items[3].document_status = "current"),
        (p) => (p.items[3].previous_issuance_id = p.items[3].id),
        (p) => (p.items[3].state = "issued"),
        (p) => (p.next_before = seq),
        (p) => p.items.pop(),
    ]) {
        const x = structuredClone(page);
        mutate(x);
        assert.equal(valid(x), false);
    }
    assert.equal(validInvoicePage(page, ledger, null, seq, 200), false);
});
test("listed detail uses historical policy and exact selected number; pending can complete, completed cannot regress", () => {
    const r = row(2);
    assert.ok(validListedInvoice(complete(), r));
    assert.ok(validListedInvoice(complete(), { ...r, state: "pending" }));
    assert.equal(validListedInvoice(pending(), r), false);
    assert.equal(validListedInvoice(complete(), { ...r, number: "other" }), false);
    assert.equal(validListedInvoice(complete(), { ...r, previous_issuance_id: null }), false);
});
test("original write and cursor read go to same-origin ledger with fixed key/JSON; mixed scope or invalid cursor is refused", async (t) => {
    const calls = [];
    const item = row(20, "9007199254740993");
    t.mock.method(globalThis, "fetch", async (path, init) => {
        calls.push({ path, init });
        return Response.json(
            init.method === "POST" ? complete() : { items: [item], next_before: null },
        );
    });
    await issueInvoice("fictional-session-csrf", ledger, cmd, "fictional-stable-key");
    assert.equal(calls[0].init.body, JSON.stringify(cmd));
    assert.equal(calls[0].init.headers["Idempotency-Key"], "fictional-stable-key");
    assert.equal(calls[0].init.headers["X-CSRF-Token"], "fictional-session-csrf");
    assert.equal(calls[0].init.credentials, "same-origin");
    const got = await listInvoiceHistory(ledger, cmd.document_id, "9007199254740994", undefined, 1);
    assert.equal(got.items[0].sequence, "9007199254740993");
    const url = new URL(calls[1].path, "https://coinpup.invalid");
    assert.equal(url.pathname, `/api/v1/ledgers/${ledger}/invoice-issuances`);
    assert.equal(url.searchParams.get("before"), "9007199254740994");
    assert.equal(url.searchParams.get("document_id"), cmd.document_id);
    await assert.rejects(listInvoiceHistory(ledger, null, "01"));
    assert.equal(calls.length, 2);
    await assert.rejects(listInvoiceHistory(id(6)), { code: "invalid_invoice_history" });
});
