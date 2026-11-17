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
    decodeReturnPositions,
    currentReturn,
    returnHistory,
    returnRevision,
    correctReturn,
    cancelReturn,
} = await import("../src/allocation-return-revision-api.ts");
test("permanent source slots keep the exact PostgreSQL BIGINT and survive freezing", () => {
    for (const raw of [
        "1",
        "9007199254740991",
        "9007199254740992",
        "9007199254740993",
        "9223372036854775807",
    ]) {
        const text =
            '{"members":[{"source_return_no":' +
            raw +
            ',"payment_amount":"0.010000000000000000"}],"reason":"quoted source_return_no"}';
        const value = decodeReturnPositions(text);
        assert.equal(value.members[0].source_return_no, raw);
        assert.equal(value.members[0].payment_amount, "0.010000000000000000");
        assert.deepEqual(JSON.parse(JSON.stringify(value)), value);
    }
    for (const x of ["-1", "0", "1.0", "1e3", "9223372036854775808", '"42"', "null", "true"]) {
        assert.throws(() => decodeReturnPositions('{"source_return_no":' + x + "}"));
    }
});
test("scoped return reads and raw revision writes preserve identifiers and request bodies", async (t) => {
    const calls = [];
    const mock = t.mock.method(globalThis, "fetch", async (path, options) => {
        calls.push([path, options]);
        return new Response('{"members":[{"source_return_no":9223372036854775807}]}', {
            status: 200,
        });
    });
    const signal = new AbortController().signal;
    const prefix =
        "/api/v1/ledgers/fictional%20ledger/business-allocation-returns/fictional%20return";
    const params = ["fictional ledger", "fictional return"];
    const body = { expected_version: 1, reason: "  Fictional 原因  " };
    for (const [answer, path] of [
        [await currentReturn(...params, signal), "/current"],
        [
            await returnRevision(...params, "fictional revision", signal),
            "/revisions/fictional%20revision",
        ],
        [await correctReturn("csrf", ...params, body, "fixed-key"), "/corrections"],
        [await cancelReturn("csrf", ...params, body, "fixed-key"), "/cancellations"],
    ]) {
        assert.equal(answer.members[0].source_return_no, "9223372036854775807");
        assert.equal(calls.shift()[0], prefix + path);
    }
    const post = mock.mock.calls[2].arguments[1];
    assert.equal(post.method, "POST");
    assert.equal(post.credentials, "same-origin");
    assert.equal(post.headers["Idempotency-Key"], "fixed-key");
    assert.equal(post.body, JSON.stringify(body));
    mock.mock.mockImplementation(async (path) => {
        assert.equal(path, prefix + "/history?limit=25&offset=25");
        return Response.json([]);
    });
    assert.deepEqual(await returnHistory(...params, 25, signal), []);
    mock.mock.mockImplementation(
        async () => new Response('{"detail":{"code":"version_conflict"}}', { status: 409 }),
    );
    await assert.rejects(
        cancelReturn("csrf", ...params, body, "fixed-key"),
        (e) => e.status === 409 && e.code === "version_conflict",
    );
    mock.mock.mockImplementation(
        async () => new Response('{"source_return_no":-1}', { status: 200 }),
    );
    await assert.rejects(
        currentReturn(...params),
        (e) => e.status === 200 && e.code === "invalid_response",
    );
});
