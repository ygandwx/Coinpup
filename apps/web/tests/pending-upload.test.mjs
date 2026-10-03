import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

registerHooks({ resolve(specifier, context, nextResolve) {
  if (specifier.startsWith(".") && !/\.[a-z]+$/u.test(specifier) && context.parentURL?.includes("/src/")) return nextResolve(`${specifier}.ts`, context);
  return nextResolve(specifier, context);
} });
const { ApiError } = await import("../src/api.ts");
const { PendingUploadController } = await import("../src/pending-upload.ts");
const { listFiles, listOperationFiles, linkFile, downloadFile } = await import("../src/document-api.ts");
const session = { user: { id: "11111111-1111-4111-8111-111111111111", username: "fictional-owner" }, csrf_token: "fictional-old-csrf" };
const other = { user: { id: "22222222-2222-4222-8222-222222222222", username: "fictional-owner" }, csrf_token: "fictional-other-csrf" };
const ledger = "33333333-3333-4333-8333-333333333333";
const fileId = "44444444-4444-4444-8444-444444444444";
const operationId = "55555555-5555-4555-8555-555555555555";
const caps = { max_upload_bytes: 1024, upload_timeout_seconds: 120, supported_media_types: ["application/pdf", "image/jpeg", "image/png", "image/webp"] };
const input = () => ({ file: new File(["%PDF-1.7 fictional exact bytes %%EOF"], "虚构发票.pdf", { type: "application/pdf" }), operationId });
const receipt = (command) => ({ upload_id: command.uploadId, file_id: fileId, link_id: command.operationId ? fileId : null, duplicate: false, sha256: "a".repeat(64), byte_size: command.byteSize, media_type: "application/pdf" });
const reservation = (command, ready = false) => ({ id: command.uploadId, ledger_id: command.ledgerId, original_filename: command.filename, declared_size: command.byteSize,
  operation_id: command.operationId, state: ready ? "ready" : "pending", created_at: "2026-01-01T00:00:00Z", completed_at: ready ? "2026-01-01T00:00:01Z" : null, response: ready ? receipt(command) : null });
function fixture(io = {}) {
  const controller = new PendingUploadController({ reserve: async (_session, command) => reservation(command), upload: async (_session, command) => receipt(command), read: async (_session, command) => reservation(command), ...io });
  controller.setOwner(session.user.id);
  return controller;
}
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }

test("committed raw upload with a lost response retries identical UUID, manifest, File and bytes", async (t) => {
  const calls = [];
  let command;
  let committed = false;
  t.mock.method(globalThis, "fetch", async (path, init) => {
    calls.push({ path, init });
    if (init.method === "POST") {
      const body = JSON.parse(init.body);
      command = { uploadId: body.id, ledgerId: ledger, filename: body.original_filename, byteSize: body.declared_size, operationId: body.operation_id };
      return Response.json(reservation(command, committed), { status: 201 });
    }
    if (!committed) { committed = true; throw new TypeError("lost after commit"); }
    return Response.json(receipt(command));
  });
  const controller = new PendingUploadController();
  controller.setOwner(session.user.id);
  const original = input();
  await controller.submit(session, ledger, original, caps);
  const first = controller.getSnapshot();
  assert.equal(first.status, "unknown");
  assert.equal(controller.dismiss(), false);
  assert.equal("file" in first.command, false);
  await controller.retry({ ...session, csrf_token: "fresh-csrf" });
  assert.equal(controller.getSnapshot().status, "confirmed");
  assert.equal(calls[0].init.body, calls[2].init.body);
  assert.equal(calls[1].path, calls[3].path);
  assert.strictEqual(calls[1].init.body, original.file);
  assert.strictEqual(calls[3].init.body, original.file);
  assert.deepEqual(await calls[1].init.body.arrayBuffer(), await calls[3].init.body.arrayBuffer());
  assert.equal(calls[3].init.headers["X-CSRF-Token"], "fresh-csrf");
  assert.equal(controller.dismiss(), true);
});

test("double clicks share one immutable command and one transport flight", async () => {
  const wait = deferred(); let count = 0;
  const controller = fixture({ reserve: async (_session, command) => { count++; await wait.promise; return reservation(command); } });
  const first = controller.submit(session, ledger, input(), caps);
  const second = controller.submit(session, ledger, input(), caps);
  assert.strictEqual(first, second);
  wait.resolve(); await first;
  assert.equal(count, 1);
});

test("reservation response loss preserves ID; pending or 404 cannot create a new intent", async () => {
  const commands = []; let reads = 0;
  const controller = fixture({ reserve: async (_session, command) => { commands.push(command); throw new ApiError("network"); },
    read: async (_session, command) => { if (++reads === 1) throw new ApiError("server", 404, "not_found"); return reservation(command); } });
  await controller.submit(session, ledger, input(), caps);
  const command = controller.getSnapshot().command;
  await controller.reconcile(session); assert.equal(controller.getSnapshot().status, "unknown");
  await controller.reconcile(session); assert.equal(controller.getSnapshot().status, "unknown");
  assert.equal(controller.dismiss(), false);
  await assert.rejects(controller.submit(session, ledger, input(), caps), { code: "pending_upload_unresolved" });
  await controller.retry(session);
  assert.strictEqual(commands[0], commands[1]);
  assert.strictEqual(controller.getSnapshot().command, command);
});

test("GET ready confirms only the matching frozen reservation and receipt", async () => {
  let changed = true;
  const controller = fixture({ upload: async () => { throw new ApiError("network"); },
    read: async (_session, command) => ({ ...reservation(command, true), ...(changed ? { original_filename: "different.pdf" } : {}) }) });
  await controller.submit(session, ledger, input(), caps);
  await controller.reconcile(session);
  assert.equal(controller.getSnapshot().status, "unknown");
  assert.equal(controller.getSnapshot().error.code, "invalid_response");
  changed = false;
  await controller.reconcile(session);
  assert.equal(controller.getSnapshot().status, "confirmed");
  assert.equal(controller.getSnapshot().receipt.file_id, fileId);
});

test("401 redacts metadata; same-owner login retains File and requires a manual retry", async () => {
  const calls = []; let failed = false;
  const original = input();
  const controller = fixture({ upload: async (current, command, file) => {
    calls.push({ current, command, file });
    if (!failed) { failed = true; throw new ApiError("unauthorized", 401); }
    return receipt(command);
  } });
  await controller.submit(session, ledger, original, caps);
  assert.equal(controller.getSnapshot().status, "auth-required");
  controller.setOwner(null);
  assert.deepEqual(controller.getSnapshot(), { status: "auth-required", command: null, receipt: null, reservation: null, error: null, checking: false });
  controller.setOwner(session.user.id);
  assert.equal(controller.getSnapshot().status, "unknown");
  assert.equal(calls.length, 1);
  await controller.retry({ ...session, csrf_token: "fresh" });
  assert.equal(calls[1].current.csrf_token, "fresh");
  assert.strictEqual(calls[1].command, calls[0].command);
  assert.strictEqual(calls[1].file, original.file);
});

test("a different owner or explicit logout cannot receive late upload state", async () => {
  const wait = deferred(); let oldCommand;
  const controller = fixture({ upload: async (_session, command) => { oldCommand = command; return wait.promise; } });
  const posting = controller.submit(session, ledger, input(), caps);
  await new Promise((resolve) => setImmediate(resolve));
  controller.setOwner(null);
  controller.setOwner(other.user.id);
  assert.equal(controller.getSnapshot().status, "idle");
  wait.resolve(receipt(oldCommand)); await posting;
  assert.equal(controller.getSnapshot().command, null);
  await assert.rejects(controller.retry(session), { kind: "unauthorized" });
  let dispatched = 0;
  const fresh = fixture({ reserve: async () => { dispatched++; throw new Error(); } });
  const queued = fresh.submit(session, ledger, input(), caps);
  fresh.setOwner(null, true); await queued;
  assert.equal(dispatched, 0);
  assert.equal(fresh.getSnapshot().status, "idle");
});

test("stop aborts transport, preserves unknown intent and allows only same-file retry", async () => {
  let first = true; const files = [];
  const controller = fixture({ upload: async (_session, command, file, options) => {
    files.push(file);
    assert.equal(options.timeoutMs, 130_000);
    if (first) { first = false; return new Promise((_resolve, reject) => options.signal.addEventListener("abort", () => reject(new ApiError("network")), { once: true })); }
    return receipt(command);
  } });
  const sending = controller.submit(session, ledger, input(), caps);
  await new Promise((resolve) => setImmediate(resolve));
  const id = controller.getSnapshot().command.uploadId;
  assert.equal(controller.stop(), true); await sending;
  assert.equal(controller.getSnapshot().status, "unknown");
  await controller.reconcile(session);
  assert.equal(controller.dismiss(), false);
  await controller.retry(session);
  assert.equal(controller.getSnapshot().command.uploadId, id);
  assert.strictEqual(files[0], files[1]);
});

test("stopping reservation prevents subsequent content dispatch even if a late reservation arrives", async () => {
  const wait = deferred(); let command; let uploads = 0;
  const controller = fixture({ reserve: async (_session, value) => { command = value; return wait.promise; }, upload: async () => { uploads++; } });
  const request = controller.submit(session, ledger, input(), caps);
  await Promise.resolve();
  assert.equal(controller.stop(), true);
  wait.resolve(reservation(command)); await request;
  assert.equal(uploads, 0);
  assert.equal(controller.getSnapshot().status, "unknown");
});

test("clear rejection permits editing, but a rejection after an unknown outcome stays locked", async () => {
  let first = true;
  const rejected = fixture({ upload: async () => { throw new ApiError("server", 415, "unsupported_file_format"); } });
  await rejected.submit(session, ledger, input(), caps);
  assert.equal(rejected.getSnapshot().status, "rejected");
  assert.equal(rejected.dismiss(), true);
  const unknown = fixture({ upload: async () => { if (first) { first = false; throw new ApiError("network"); } throw new ApiError("server", 409, "entity_archived"); } });
  await unknown.submit(session, ledger, input(), caps); await unknown.retry(session);
  assert.equal(unknown.getSnapshot().status, "unknown");
  assert.equal(unknown.dismiss(), false);
});

test("content conflicts cannot be dismissed or confirmed by a matching manifest alone", async () => {
  let expire = true;
  const controller = fixture({ upload: async () => { throw new ApiError("server", 409, "upload_content_conflict"); }, read: async (_session, command) => {
    if (expire) { expire = false; throw new ApiError("unauthorized", 401); }
    return reservation(command, true);
  } });
  await controller.submit(session, ledger, input(), caps);
  await controller.reconcile(session);
  controller.setOwner(null); controller.setOwner(session.user.id);
  assert.equal(controller.getSnapshot().status, "upload-conflict");
  await controller.reconcile(session);
  assert.equal(controller.getSnapshot().status, "upload-conflict");
  assert.equal(controller.getSnapshot().receipt, null);
  assert.equal(controller.dismiss(), false);
  await assert.rejects(controller.retry(session), { code: "pending_retry_unavailable" });
});

test("malformed JSON or completion identity never turns into a confirmed upload", async () => {
  for (const change of [{ upload_id: fileId }, { byte_size: 0 }, { sha256: "a".repeat(64) + "\n" }, { link_id: null }, { duplicate: "false" }]) {
    const controller = fixture({ upload: async (_session, command) => ({ ...receipt(command), ...change }) });
    await controller.submit(session, ledger, input(), caps);
    assert.equal(controller.getSnapshot().status, "unknown");
    assert.equal(controller.getSnapshot().error.code, "invalid_response");
  }
});

test("snapshots are stable and capabilities reject impossible inputs before generating an ID", async () => {
  const controller = fixture();
  const empty = controller.getSnapshot();
  controller.setOwner(session.user.id); assert.strictEqual(controller.getSnapshot(), empty);
  await assert.rejects(controller.submit(session, ledger, { file: new File([], "empty.pdf") }, caps), { code: "file_too_large" });
  await assert.rejects(controller.submit(session, ledger, input(), { ...caps, upload_timeout_seconds: Infinity }), { code: "invalid_configuration" });
  await assert.rejects(controller.submit(session, ledger, { file: new File(["x"], "../unsafe.pdf") }, caps), { code: "invalid_filename" });
  assert.equal(controller.getSnapshot().command, null);
});

test("document lists page explicitly; links use PUT and downloads reject HTML without losing 401", async (t) => {
  const calls = []; let reply = [];
  t.mock.method(globalThis, "fetch", async (path, init) => { calls.push({ path, init }); return reply instanceof Response ? reply : Response.json(reply); });
  await listFiles(ledger, { include_archived: true, limit: 2, offset: 4 });
  await listOperationFiles(ledger, operationId, { limit: 3, offset: 6 });
  await linkFile("csrf", ledger, operationId, fileId);
  assert.ok(calls[0].path.endsWith("?limit=2&offset=4&include_archived=true"));
  assert.ok(calls[1].path.endsWith("?limit=3&offset=6"));
  assert.equal(calls[2].init.method, "PUT");
  assert.equal(calls[2].init.body, undefined);
  await assert.rejects(listFiles(ledger, { limit: 201 }), { code: "invalid_pagination" });
  reply = new Response("<html>not a file</html>", { headers: { "content-type": "text/html" } });
  await assert.rejects(downloadFile(ledger, fileId), { code: "invalid_response" });
  reply = new Response("not-json", { status: 401 });
  await assert.rejects(downloadFile(ledger, fileId), { kind: "unauthorized" });
});
