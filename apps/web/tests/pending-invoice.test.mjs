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
const { PendingInvoiceController } = await import("../src/pending-invoice.ts");
const { ApiError } = await import("../src/api.ts");
const sample = JSON.parse(
    readFileSync(new URL("./document-current-fixture.json", import.meta.url), "utf8"),
);
const id = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const ledger = sample.current.ledger_id,
    owner = id(1),
    session = { user: { id: owner }, csrf_token: "fictional-csrf" };
const profile = () => ({
    id: id(2),
    ledger_id: ledger,
    archived: false,
    version: 3,
    created_at: "2026-11-01T00:00:00Z",
    updated_at: "2026-11-01T00:00:00Z",
    name: "虚构公司 Fictional",
    numbering: { prefix: "F-", minimum_digits: 3 },
    issuer_extra_text: null,
    payment_instructions: "Fictional",
    footer: "示例",
    logo_file_id: null,
    logo_file_version: null,
});
const review = () => ({
    ...structuredClone(sample),
    profile: profile(),
    latest: {
        items: [
            {
                id: id(3),
                ledger_id: ledger,
                document_id: sample.current.document_id,
                document_version: 3,
                previous_issuance_id: null,
                sequence: "9",
                number: "OLD-9",
                state: "pending",
                current_document_version: sample.current.version,
                document_status: "superseded",
            },
        ],
        next_before: null,
    },
});
function response(plan, pending = false) {
    const body = JSON.parse(plan.bodyJson),
        { profile } = JSON.parse(plan.reviewedJson);
    return {
        kind: "invoice_issuance",
        id: body.id,
        ledger_id: plan.ledgerId,
        document_id: body.document_id,
        document_version: body.expected_document_version,
        configuration_id: body.configuration_id,
        configuration_version: body.expected_configuration_version,
        previous_issuance_id: body.previous_issuance_id,
        numbering: profile.numbering,
        number: "F-010",
        sequence: "10",
        snapshot_sha256: "a".repeat(64),
        template_version: "commercial-invoice-v1",
        state: pending ? "pending" : "complete",
        artifacts: pending
            ? []
            : ["zh", "en"].map((language, i) => ({
                  language,
                  file_id: id(i + 5),
                  sha256: String(i + 1).repeat(64),
                  byte_size: 801 + i,
                  media_type: "application/pdf",
              })),
    };
}
test("lost response preserves original owner, body/key, reviewed lines and prior number; even a complete GET cannot confirm", async () => {
    const sends = [],
        reads = [];
    let mode = "pending",
        attempt = 0;
    const ctl = new PendingInvoiceController(
        async (s, p) => {
            sends.push([s.csrf_token, p]);
            attempt++;
            if (attempt === 1) throw new ApiError("network");
            if (attempt === 2) throw new ApiError("server", 409, "version_conflict");
            const v = response(p);
            if (attempt === 3) v.artifacts[1].file_id = id(99);
            return v;
        },
        async (p) => {
            reads.push(p);
            return response(p, mode === "pending");
        },
    );
    ctl.setOwner(owner);
    const input = review();
    const saved = structuredClone(input);
    await ctl.start(session, ledger, input);
    input.current.document.lines[0].description = "Edited after review";
    input.profile.numbering.prefix = "NEW";
    input.latest.items = [];
    assert.equal(ctl.getSnapshot().status, "unknown");
    assert.equal(ctl.dismiss(), false);
    await assert.rejects(ctl.start(session, ledger, review()));
    await ctl.inspect(session);
    assert.equal(ctl.getSnapshot().observed.state, "pending");
    assert.equal(ctl.getSnapshot().receipt, null);
    mode = "complete";
    await ctl.inspect(session);
    assert.equal(ctl.getSnapshot().observed.state, "complete");
    assert.equal(ctl.getSnapshot().status, "unknown");
    assert.equal(ctl.getSnapshot().receipt, null);
    await ctl.retry(session);
    assert.equal(ctl.getSnapshot().status, "unknown");
    await ctl.retry(session);
    assert.equal(ctl.getSnapshot().status, "unknown");
    assert.equal(ctl.getSnapshot().error.code, "invalid_response");
    await ctl.retry({ ...session, csrf_token: "fictional-renewed" });
    assert.equal(ctl.getSnapshot().status, "confirmed");
    assert.ok(Object.isFrozen(ctl.getSnapshot().receipt.artifacts[0]));
    assert.deepEqual(JSON.parse(sends[0][1].reviewedJson), saved);
    assert.equal(JSON.parse(sends[0][1].bodyJson).previous_issuance_id, id(3));
    assert.ok(sends.every(([, p]) => p === sends[0][1]) && reads.every((p) => p === sends[0][1]));
    assert.equal(sends.at(-1)[0], "fictional-renewed");
    assert.ok(!JSON.stringify(sends[0][1]).includes(session.csrf_token));
    assert.ok(ctl.dismiss());
});
test("missing input, cross-ledger facts, wrong profile, current/cancelled predecessor or changed prices never send", async () => {
    let posts = 0;
    const c = new PendingInvoiceController(async () => {
        posts++;
        throw new Error();
    });
    c.setOwner(owner);
    for (const mutate of [
        (r) => (r.asset.scale = -2),
        (r) => (r.current.document_kind = "bill"),
        (r) => (r.current.document.lines[0].net_amount = "1234"),
        (r) => (r.profile.logo_file_version = 1),
        (r) => (r.profile.ledger_id = id(80)),
        (r) => (r.profile.archived = true),
        (r) => (r.profile.numbering.minimum_digits = 0),
        (r) => (r.latest.items[0].document_status = "cancelled"),
        (r) => r.latest.items[0].current_document_version++,
        (r) => {
            r.latest.items[0].document_version = r.current.version;
            r.latest.items[0].document_status = "current";
        },
        (r) => r.latest.items.push(r.latest.items[0]),
        (r) => (r.extra = "unexpected"),
    ]) {
        const v = review();
        mutate(v);
        await assert.rejects(c.start(session, ledger, v), { code: "invalid_invoice_intent" });
    }
    assert.equal(posts, 0);
    assert.equal(c.getSnapshot().status, "idle");
});
test("only explicit same-owner replay restores hidden 401; pending POST and malformed pair remain unknown", async () => {
    let n = 0;
    const c = new PendingInvoiceController(async (s, p) => {
        n++;
        if (n === 1) throw new ApiError("unauthorized", 401);
        const v = response(p, n === 2);
        if (n === 3) v.artifacts.reverse();
        return v;
    });
    c.setOwner(owner);
    const first = review();
    first.latest = { items: [], next_before: null };
    await c.start(session, ledger, first);
    assert.equal(c.getSnapshot().status, "auth-required");
    assert.equal(c.getSnapshot().plan, null);
    await assert.rejects(c.retry(session));
    c.setOwner(owner);
    assert.equal(c.getSnapshot().status, "unknown");
    assert.equal(JSON.parse(c.getSnapshot().plan.bodyJson).previous_issuance_id, null);
    await c.retry(session);
    assert.equal(c.getSnapshot().status, "unknown");
    await c.retry(session);
    assert.equal(c.getSnapshot().status, "unknown");
    await c.retry(session);
    assert.equal(c.getSnapshot().status, "confirmed");
    c.setOwner(null, true);
    assert.equal(c.getSnapshot().status, "idle");
    assert.equal(c.getSnapshot().receipt, null);
});
test("competing key stays conflicted, including 401 during inspection; known initial rejection allows a new review", async () => {
    let m = 0;
    const c = new PendingInvoiceController(
        async () => {
            m++;
            throw new ApiError(
                "server",
                409,
                m === 1 ? "invoice_predecessor_conflict" : "idempotency_conflict",
            );
        },
        async () => {
            throw new ApiError("unauthorized", 401);
        },
    );
    c.setOwner(owner);
    await c.start(session, ledger, review());
    assert.equal(c.getSnapshot().status, "rejected");
    assert.ok(c.dismiss());
    await c.start(session, ledger, review());
    assert.equal(c.getSnapshot().status, "idempotency-conflict");
    await c.inspect(session);
    assert.equal(c.getSnapshot().plan, null);
    c.setOwner(owner);
    assert.equal(c.getSnapshot().status, "idempotency-conflict");
    await assert.rejects(c.retry(session));
    assert.equal(m, 2);
});
test("only definite pre-reservation errors release a first Invoice intent", async () => {
    for (const [status, code] of [
        [409, "file_archived"],
        [409, "invoice_file_choice_changed"],
        [422, "invoice_render_logo_limit"],
        [503, "invoice_render_logo_invalid"],
        [409, "constraint_conflict"],
        [422, "validation_error"],
    ]) {
        const sent = [];
        const c = new PendingInvoiceController(async (_session, plan) => {
            sent.push(plan);
            if (sent.length === 1) throw new ApiError("server", status, code);
            return response(plan);
        });
        c.setOwner(owner);
        await c.start(session, ledger, review());
        assert.equal(c.getSnapshot().status, "unknown", code);
        assert.equal(c.getSnapshot().error.code, code);
        assert.equal(c.getSnapshot().receipt, null);
        const original = c.getSnapshot().plan;
        assert.equal(c.dismiss(), false);
        await assert.rejects(c.start(session, ledger, review()), {
            code: "pending_invoice_unresolved",
        });

        await c.retry(session);
        assert.equal(c.getSnapshot().status, "confirmed");
        assert.equal(sent.length, 2);
        assert.equal(sent[1].key, original.key);
        assert.equal(sent[1].bodyJson, original.bodyJson);
        assert.equal(c.getSnapshot().receipt.id, JSON.parse(original.bodyJson).id);
    }

    for (const [status, code] of [
        [409, "version_conflict"],
        [422, "invoice_logo_size_limit"],
        [422, "invoice_logo_invalid"],
    ]) {
        const sent = [];
        const c = new PendingInvoiceController(async (_session, plan) => {
            sent.push(plan);
            if (sent.length === 1) throw new ApiError("server", status, code);
            return response(plan);
        });
        c.setOwner(owner);
        await c.start(session, ledger, review());
        assert.equal(c.getSnapshot().status, "rejected", code);
        assert.equal(c.getSnapshot().error.code, code);
        await assert.rejects(c.retry(session), { code: "pending_invoice_unresolved" });
        assert.equal(c.dismiss(), true);
        await c.start(session, ledger, review());
        assert.equal(c.getSnapshot().status, "confirmed");
        assert.equal(sent.length, 2);
        assert.notEqual(sent[0].key, sent[1].key);
    }
});
test("duplicate clicks, owner changes and explicit logout isolate late write and read results", async () => {
    for (const read of [false, true])
        for (const logout of [false, true]) {
            let release;
            const delayed = new Promise((r) => {
                release = r;
            });
            let captured;
            const c = new PendingInvoiceController(
                async (s, p) => {
                    captured = p;
                    if (read) throw new ApiError("network");
                    return delayed;
                },
                () => delayed,
            );
            c.setOwner(owner);
            let flight = c.start(session, ledger, review());
            if (read) {
                await flight;
                flight = c.inspect(session);
            }
            await Promise.resolve();
            await assert.rejects(c.start(session, ledger, review()));
            await assert.rejects(c.retry(session));
            c.setOwner(logout ? null : id(15), logout);
            release(response(captured));
            await flight;
            assert.equal(c.getSnapshot().status, "idle");
            assert.equal(c.getSnapshot().observed, null);
            assert.equal(c.getSnapshot().receipt, null);
        }
});
