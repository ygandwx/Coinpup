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
const { PendingInvoiceProfiles } = await import("../src/pending-invoice-profiles.ts");
const { ApiError } = await import("../src/api.ts");
const id = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const ledger = id(1),
    owner = id(2),
    identifier = id(3),
    session = { user: { id: owner }, csrf_token: "fictional-profile-token" };
const fields = () => ({
    name: " Fictional 名称 ",
    numbering: { prefix: "示例-", minimum_digits: 4 },
    logo_file_id: null,
    logo_file_version: null,
    footer: "  Fictional  ",
    issuer_extra_text: null,
    payment_instructions: null,
});
const row = (version = 1) => ({
    ...fields(),
    name: "Fictional 名称",
    id: identifier,
    ledger_id: ledger,
    version,
    archived: false,
    created_at: "2026-11-01T00:00:00Z",
    updated_at: "2026-11-01T00:00:00Z",
});
const create = () => ({ id: identifier, ...fields() });
const ctl = (send, read) => {
    const c = new PendingInvoiceProfiles(send, read);
    c.setOwner(owner);
    return c;
};
for (const exact of [true, false])
    test(`create lost, duplicate ID, read exact fields including numbering and raw spaces: ${exact}`, async () => {
        const seen = [];
        const c = ctl(
            async (s, p) => {
                seen.push(p);
                if (seen.length === 1) throw new ApiError("network");
                throw new ApiError("server", 409, "duplicate_record");
            },
            async () => {
                const v = row();
                if (!exact) v.numbering.prefix = "Other";
                return v;
            },
        );
        const body = create();
        await c.start(session, ledger, identifier, "create", body, null);
        body.numbering.minimum_digits = 9;
        assert.equal(c.getSnapshot().status, "unknown");
        await c.retry(session);
        assert.equal(c.getSnapshot().status, exact ? "confirmed" : "conflict");
        assert.equal(seen[0], seen[1]);
        assert.equal(JSON.parse(seen[1].bodyJson).numbering.minimum_digits, 4);
        assert.ok(Object.isFrozen(c.getSnapshot().record.numbering));
    });
test("unknown replacement with higher version is a conflict even if every editable field matches, never an implied success", async () => {
    let n = 0,
        v = 1;
    const c = ctl(
        async () => {
            if (n++ === 0) throw new ApiError("network");
            throw new ApiError("server", 409, "version_conflict");
        },
        async () => row(v),
    );
    await c.start(
        session,
        ledger,
        identifier,
        "replace",
        { expected_version: 1, ...fields() },
        row(),
    );
    await c.retry(session);
    assert.equal(c.getSnapshot().status, "unknown");
    assert.equal(c.dismiss(), false);
    v = 2;
    await c.retry(session);
    assert.equal(c.getSnapshot().status, "conflict");
    assert.equal(c.getSnapshot().record.version, 2);
});
test("reviewed Logo must be a scoped active pinned image; retaining its saved identity and version does not need a new file selection", async () => {
    let posts = 0;
    const c = ctl(async () => {
        posts++;
        throw new ApiError("network");
    });
    const existing = row();
    existing.logo_file_id = id(4);
    existing.logo_file_version = 3;
    const body = { expected_version: 1, ...fields(), logo_file_id: id(5), logo_file_version: 2 },
        file = {
            id: id(5),
            version: 2,
            ledger_id: ledger,
            archived: false,
            detected_media_type: "image/png",
        };
    for (const bad of [
        null,
        { ...file, id: id(6) },
        { ...file, ledger_id: id(7) },
        { ...file, version: 1 },
        { ...file, detected_media_type: "application/pdf" },
        { ...file, archived: true },
    ]) {
        await assert.rejects(c.start(session, ledger, identifier, "replace", body, existing, bad), {
            code: "invalid_invoice_profile_intent",
        });
    }
    assert.equal(posts, 0);
    await c.start(
        session,
        ledger,
        identifier,
        "replace",
        { ...body, logo_file_id: id(4), logo_file_version: 3 },
        existing,
    );
    assert.equal(posts, 1);
});
test("PUT, archive and restore use original scoped methods, explicit expected version and current CSRF; incorrect fields remain unknown", async (t) => {
    let previous = row();
    const calls = [];
    t.mock.method(globalThis, "fetch", async (path, init) => {
        calls.push([path, init]);
        const body = JSON.parse(init.body);
        let next = { ...previous, version: previous.version + 1 };
        if (init.method === "PUT") next = { ...next, name: "Changed 名称", footer: "new" };
        else next.archived = body.archived;
        previous = next;
        return Response.json(next);
    });
    const c = ctl(),
        body = { expected_version: 1, ...fields(), name: " Changed 名称 ", footer: "new" };
    await c.start(session, ledger, identifier, "replace", body, previous);
    assert.equal(c.getSnapshot().status, "confirmed");
    await c.start(
        session,
        ledger,
        identifier,
        "archive",
        { expected_version: 2, archived: true },
        previous,
    );
    assert.equal(c.getSnapshot().record.archived, true);
    await c.start(
        session,
        ledger,
        identifier,
        "archive",
        { expected_version: 3, archived: false },
        previous,
    );
    assert.equal(c.getSnapshot().record.archived, false);
    assert.deepEqual(
        calls.map(([u, i]) => [u, i.method]),
        [
            [`/api/v1/ledgers/${ledger}/invoice-profiles/${identifier}`, "PUT"],
            ...Array(2).fill([
                `/api/v1/ledgers/${ledger}/invoice-profiles/${identifier}/archive`,
                "PATCH",
            ]),
        ],
    );
    assert.ok(
        calls.every(
            ([, i]) =>
                i.headers["X-CSRF-Token"] === session.csrf_token && !i.headers["Idempotency-Key"],
        ),
    );
});
test("401 and owner replacement discard late profile responses after reauthentication", async () => {
    let release;
    const c = ctl(
        async () =>
            new Promise((r) => {
                release = r;
            }),
    );
    const p = c.start(session, ledger, identifier, "create", create(), null);
    await Promise.resolve();
    c.setOwner(null);
    assert.equal(c.getSnapshot().plan, null);
    c.setOwner(owner);
    assert.equal(c.getSnapshot().status, "unknown");
    c.setOwner(id(11));
    release(row());
    await p;
    assert.equal(c.getSnapshot().status, "idle");
    assert.equal(c.getSnapshot().record, null);
});
