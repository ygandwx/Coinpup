import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiError, checkHealth, getSession, readBlob, readJson, signIn, signOut, writeBytes, writeJson } from "../src/api.ts";

test("write preserves string amounts, request-local CSRF and persistent idempotency headers", async (t) => {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (path, init) => {
    calls.push({ path, init });
    return Response.json({ amount: "99999999999999999999.999999999999999999" });
  });
  const amount = "99999999999999999999.999999999999999999";
  assert.deepEqual(await writeJson("/api/v1/example", "fictional-csrf", { amount }, "PATCH", "fictional-key"), { amount });
  assert.equal(calls[0].init.body, JSON.stringify({ amount }));
  assert.equal(calls[0].init.method, "PATCH");
  assert.equal(calls[0].init.headers["X-CSRF-Token"], "fictional-csrf");
  assert.equal(calls[0].init.headers["Idempotency-Key"], "fictional-key");
  assert.equal(calls[0].init.credentials, "same-origin");
  assert.equal(calls[0].init.cache, "no-store");
  assert.equal(calls[0].init.redirect, "error");
  await readJson("/api/v1/example");
  assert.equal(calls[1].init.headers["X-CSRF-Token"], undefined);
  assert.equal(calls[1].init.headers["Idempotency-Key"], undefined);
});

test("API paths cannot send CSRF credentials to another origin or outside the API", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => Response.json({}));
  for (const path of ["https://example.invalid/api/v1/assets", "//example.invalid/api/v1/assets", "/api/v10/assets", "/api/v1/../../auth", "/api/v1/%2e%2e/%2e%2e/auth", "/api/v1/%2e%2e%2f%2e%2e%2fauth", "/api/v1/..\\..\\auth", "/api/v1/assets#fragment"]) {
    await assert.rejects(writeJson(path, "fictional-csrf", {}), { code: "invalid_api_path" });
  }
  assert.equal(fetch.mock.callCount(), 0);
});

test("HTTP domain codes and field locations survive without retaining submitted input", async (t) => {
  const replies = [
    Response.json({ detail: { code: "version_conflict", message: "Record changed" } }, { status: 409 }),
    Response.json({ detail: [{ loc: ["body", "asset_ids", 1], msg: "Invalid asset", type: "value_error", input: "fictional-private-input", ctx: { error: "private" } }] }, { status: 422 }),
    Response.json({ detail: "Expired" }, { status: 401 }),
    Response.json({ detail: "CSRF rejected" }, { status: 403 }),
    Response.json({ detail: "Retry later" }, { status: 429 }),
  ];
  t.mock.method(globalThis, "fetch", async () => replies.shift());
  await assert.rejects(readJson("/api/v1/example"), { status: 409, code: "version_conflict", kind: "server" });
  await assert.rejects(readJson("/api/v1/example"), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 422);
    assert.deepEqual(error.fieldErrors, [{ path: ["body", "asset_ids", 1], message: "Invalid asset", type: "value_error" }]);
    assert.ok(!JSON.stringify(error).includes("fictional-private-input"));
    return true;
  });
  await assert.rejects(readJson("/api/v1/example"), { kind: "unauthorized", status: 401 });
  await assert.rejects(readJson("/api/v1/example"), { kind: "forbidden", status: 403 });
  await assert.rejects(readJson("/api/v1/example"), { kind: "rate-limited", status: 429 });
});

test("non-JSON authentication errors retain HTTP classification without raw response content", async (t) => {
  const mock = t.mock.method(globalThis, "fetch", async () => new Response(null, { status: 401 }));
  for (const [status, kind] of [[401, "unauthorized"], [403, "forbidden"], [429, "rate-limited"]]) {
    for (const body of [null, "<html>fictional-private-proxy-detail</html>"]) {
      mock.mock.mockImplementation(async () => new Response(body, { status }));
      await assert.rejects(readJson("/api/v1/example"), (error) => {
        assert.ok(error instanceof ApiError);
        assert.equal(error.kind, kind);
        assert.equal(error.status, status);
        assert.equal(error.code, null);
        assert.deepEqual(error.fieldErrors, []);
        assert.ok(!JSON.stringify(error).includes("fictional-private-proxy-detail"));
        assert.ok(!error.message.includes("fictional-private-proxy-detail"));
        return true;
      });
    }
  }
});

test("malformed JSON, connection failures and cancellation are classified", async (t) => {
  const mock = t.mock.method(globalThis, "fetch", async () => new Response("broken JSON"));
  await assert.rejects(readJson("/api/v1/example"), { kind: "server", code: "invalid_response" });
  mock.mock.mockImplementation(async () => { throw new TypeError("offline"); });
  await assert.rejects(readJson("/api/v1/example"), { kind: "network" });
  mock.mock.mockImplementation(async (_path, init) => {
    assert.equal(init.signal.aborted, true);
    throw new DOMException("Aborted", "AbortError");
  });
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(readJson("/api/v1/example", controller.signal), { kind: "network" });
});

test("existing authentication validates sessions and accepts an empty logout response", async (t) => {
  const session = { user: { id: "fictional-user", username: "fictional-admin" }, csrf_token: "fictional-token" };
  const calls = [];
  const replies = [Response.json(session), Response.json(session), new Response(null, { status: 204 }), Response.json({ status: "ok" }), Response.json({ user: {}, csrf_token: "" })];
  t.mock.method(globalThis, "fetch", async (path, init) => { calls.push({ path, init }); return replies.shift(); });
  assert.deepEqual(await getSession(), session);
  assert.deepEqual(await signIn("fictional-admin", "fictional-password"), session);
  assert.equal(calls[1].init.headers["X-CSRF-Token"], undefined);
  assert.equal(await signOut("fictional-token"), undefined);
  assert.equal(await checkHealth("ready"), true);
  await assert.rejects(getSession(), { kind: "server", code: "invalid_response" });
});

test("raw uploads preserve the original Blob, configurable timeout and protected request options", async (t) => {
  const file = new File(["fictional raw bytes"], "fictional.pdf", { type: "application/pdf" });
  const timeouts = [];
  const schedule = globalThis.setTimeout;
  t.mock.method(globalThis, "setTimeout", (callback, delay, ...args) => { timeouts.push(delay); return schedule(callback, delay, ...args); });
  const calls = [];
  t.mock.method(globalThis, "fetch", async (path, init) => { calls.push({ path, init }); return Response.json({ ready: true }); });
  assert.deepEqual(await writeBytes("/api/v1/example", "fictional-csrf", file, { timeoutMs: 130_000 }), { ready: true });
  assert.strictEqual(calls[0].init.body, file);
  assert.equal(calls[0].init.method, "PUT");
  assert.equal(calls[0].init.headers["Content-Type"], "application/octet-stream");
  assert.equal(calls[0].init.headers["X-CSRF-Token"], "fictional-csrf");
  assert.equal(calls[0].init.credentials, "same-origin");
  assert.equal(calls[0].init.redirect, "error");
  assert.equal(calls[0].init.cache, "no-store");
  assert.deepEqual(timeouts, [130_000]);
  await assert.rejects(writeBytes("https://attacker.invalid/api/v1/file", "secret", file, { timeoutMs: 130_000 }), { code: "invalid_api_path" });
  await assert.rejects(writeBytes("/api/v1/file", "secret", file, { timeoutMs: Infinity }), { code: "invalid_timeout" });
  assert.equal(calls.length, 1);
});

test("private Blob downloads preserve authentication errors and binary bytes", async (t) => {
  const replies = [new Response("<html>private-login-page</html>", { status: 401 }), new Response("fictional-pdf", { headers: { "content-type": "application/pdf" } })];
  t.mock.method(globalThis, "fetch", async (_path, init) => {
    assert.equal(init.headers["X-CSRF-Token"], undefined);
    assert.equal(init.credentials, "same-origin");
    return replies.shift();
  });
  await assert.rejects(readBlob("/api/v1/file"), { kind: "unauthorized", status: 401 });
  const blob = await readBlob("/api/v1/file");
  assert.equal(blob.type, "application/pdf");
  assert.equal(await blob.text(), "fictional-pdf");
});

test("raw and JSON requests honor external cancellation without replacing their bodies", async (t) => {
  t.mock.method(globalThis, "fetch", async (_path, init) => new Promise((_resolve, reject) => {
    const fail = () => reject(new DOMException("Aborted", "AbortError"));
    if (init.signal.aborted) fail(); else init.signal.addEventListener("abort", fail, { once: true });
  }));
  const abort = new AbortController();
  const upload = writeBytes("/api/v1/file", "csrf", new Blob(["fixture"]), { timeoutMs: 130_000, signal: abort.signal });
  abort.abort();
  await assert.rejects(upload, { kind: "network" });
  await assert.rejects(writeJson("/api/v1/reservation", "csrf", { id: "fixture" }, "POST", undefined, abort.signal), { kind: "network" });
});
