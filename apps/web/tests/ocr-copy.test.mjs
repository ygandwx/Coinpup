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
const { OcrCopyController } = await import("../src/ocr-copy.ts");
const { ApiError } = await import("../src/api.ts");
const id = (n) => `${String(n).repeat(8)}-1111-4111-8111-111111111111`;
const session = { user: { id: id(1) }, csrf_token: "fictional-old" };
const source = { ledger: id(2), draft: id(3), file: id(4), targetLedger: id(5) };
const blob = new Blob(["%PDF fictional copy"], { type: "application/pdf" });
const metadata = {
    id: source.file,
    ledger_id: source.ledger,
    original_filename: "虚构.pdf",
    byte_size: blob.size,
    detected_media_type: blob.type,
    sha256: "a".repeat(64),
    archived: false,
};
const config = { max_upload_bytes: 1024, upload_timeout_seconds: 120 };
const receipt = (command) => ({
    upload_id: command.uploadId,
    file_id: id(6),
    link_id: null,
    duplicate: false,
    sha256: metadata.sha256,
    byte_size: blob.size,
    media_type: blob.type,
});
const reservation = (command) => ({
    id: command.uploadId,
    ledger_id: command.ledgerId,
    original_filename: command.filename,
    declared_size: command.byteSize,
    operation_id: null,
    state: "pending",
    response: null,
    created_at: "2031-01-01",
    completed_at: null,
});
function fixture(reads = {}, transport = {}) {
    const calls = [];
    const controller = new OcrCopyController(
        {
            metadata: async () => metadata,
            content: async () => blob,
            configuration: async () => config,
            ...reads,
        },
        {
            reserve: async (_session, command) => reservation(command),
            read: async (_session, command) => reservation(command),
            upload: async (auth, command, file) => {
                calls.push({ auth, command, file });
                return receipt(command);
            },
            ...transport,
        },
    );
    controller.setOwner(session.user.id);
    return { controller, calls };
}
function deferred() {
    let resolve;
    const promise = new Promise((yes) => {
        resolve = yes;
    });
    return { promise, resolve };
}

test("copy preserves source context, exact bytes and filename without a financial link", async () => {
    const { controller, calls } = fixture();
    const mutable = { ...source };
    const flight = controller.start(session, mutable);
    mutable.targetLedger = id(9);
    await flight;
    const state = controller.getSnapshot();
    assert.equal(state.status, "confirmed");
    assert.equal(state.context.targetLedger, source.targetLedger);
    assert.equal(state.context.draft, source.draft);
    assert.equal(calls[0].command.ledgerId, source.targetLedger);
    assert.equal(calls[0].command.operationId, null);
    assert.equal(calls[0].file.name, metadata.original_filename);
    assert.equal(await calls[0].file.text(), await blob.text());
    assert.equal(JSON.stringify(state).includes("%PDF"), false);
    assert.equal(controller.dismiss(), true);
    assert.equal(controller.getSnapshot().context, null);
});

test("lost upload response and same-owner login retry the same File and UUID with fresh CSRF", async () => {
    const calls = [];
    const { controller } = fixture(
        {},
        {
            upload: async (auth, command, file) => {
                calls.push({ auth, command, file });
                if (calls.length === 1) throw new ApiError("network");
                return receipt(command);
            },
        },
    );
    await controller.start(session, source);
    assert.equal(controller.getSnapshot().status, "unknown");
    assert.equal(controller.dismiss(), false);
    controller.setOwner(null);
    assert.equal(controller.getSnapshot().context, null);
    assert.equal(controller.getSnapshot().upload.command, null);
    controller.setOwner(session.user.id);
    await controller.retry({ ...session, csrf_token: "fictional-new" });
    assert.equal(controller.getSnapshot().status, "confirmed");
    assert.equal(calls[0].file, calls[1].file);
    assert.equal(calls[0].command, calls[1].command);
    assert.equal(calls[1].auth.csrf_token, "fictional-new");
});

test("wrong copy receipt identity is unknown and retains original bytes for retry", async () => {
    let calls = 0;
    const files = [];
    const { controller } = fixture(
        {},
        {
            upload: async (_auth, command, file) => {
                files.push(file);
                calls += 1;
                return { ...receipt(command), sha256: (calls === 1 ? "b" : "a").repeat(64) };
            },
        },
    );
    await controller.start(session, source);
    assert.equal(controller.getSnapshot().status, "unknown");
    assert.equal(controller.getSnapshot().upload.receipt, null);
    await controller.retry(session);
    assert.equal(controller.getSnapshot().status, "confirmed");
    assert.equal(files[0], files[1]);
});

test("logout or owner switch fences late source reads before any upload", async () => {
    for (const explicit of [true, false]) {
        const gate = deferred();
        const { controller, calls } = fixture({ content: () => gate.promise });
        const pending = controller.start(session, source);
        await new Promise(setImmediate);
        controller.setOwner(explicit ? null : id(9), explicit);
        gate.resolve(blob);
        await pending;
        assert.equal(calls.length, 0);
        assert.equal(controller.getSnapshot().status, "idle");
        assert.equal(controller.getSnapshot().context, null);
    }
});

test("session expiry while reading preserves context but cannot upload until manual retry", async () => {
    const gate = deferred();
    let count = 0;
    const { controller, calls } = fixture({
        content: () => (++count === 1 ? gate.promise : Promise.resolve(blob)),
    });
    const pending = controller.start(session, source);
    await new Promise(setImmediate);
    controller.setOwner(null);
    gate.resolve(blob);
    await pending;
    assert.equal(calls.length, 0);
    assert.equal(controller.getSnapshot().context, null);
    controller.setOwner(session.user.id);
    assert.equal(controller.getSnapshot().status, "read-failed");
    assert.equal(controller.getSnapshot().context.draft, source.draft);
    await controller.retry(session);
    assert.equal(calls.length, 1);
    assert.equal(controller.getSnapshot().status, "confirmed");
});

test("owner switch fences late committed upload responses", async () => {
    const gate = deferred();
    let command;
    const { controller } = fixture(
        {},
        {
            upload: async (_auth, value) => {
                command = value;
                return gate.promise;
            },
        },
    );
    const pending = controller.start(session, source);
    await new Promise(setImmediate);
    controller.setOwner(id(9));
    gate.resolve(receipt(command));
    await pending;
    assert.equal(controller.getSnapshot().status, "idle");
    assert.equal(controller.getSnapshot().upload.receipt, null);
});

test("source mismatch, archive, size and content mismatch fail before reservation", async () => {
    for (const changes of [
        { id: id(8) },
        { ledger_id: id(8) },
        { archived: true },
        { byte_size: 1025 },
    ]) {
        const { controller, calls } = fixture({
            metadata: async () => ({ ...metadata, ...changes }),
        });
        await controller.start(session, source);
        assert.equal(controller.getSnapshot().status, "read-failed");
        assert.equal(controller.getSnapshot().upload.command, null);
        assert.equal(calls.length, 0);
    }
    const { controller } = fixture({
        content: async () => new Blob(["wrong"], { type: blob.type }),
    });
    await controller.start(session, source);
    assert.equal(controller.getSnapshot().status, "read-failed");
    assert.equal(controller.getSnapshot().upload.command, null);
});
