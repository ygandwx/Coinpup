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

const { PendingPrice } = await import("../src/pending-price.ts");
const owner = { user: { id: "fictional" }, csrf_token: "fictional-csrf-before" };
const fresh = { ...owner, csrf_token: "fictional-csrf-after" };
const quote = {
    asset_id: "EUR",
    display_asset_id: "USD",
    price_date: "2026-07-01",
    price: "0.9900010000",
    source: "Fictional price reference",
    expected_version: 1,
};
const response = {
    asset_id: quote.asset_id,
    display_asset_id: quote.display_asset_id,
    price_date: quote.price_date,
    price: "0.990001",
    source: quote.source,
    version: 2,
    id: "00000000-0000-4000-8000-000000000001",
    updated_at: "2026-07-01T00:00:00Z",
};

test("same-owner price recovery resends the frozen decimal and version after session loss", async (t) => {
    const requests = [];
    t.mock.method(globalThis, "fetch", async (_route, options) => {
        requests.push(options);
        if (requests.length === 1) throw new Error("Lost connection");
        if (requests.length === 2) return Response.json(null, { status: 401 });
        return Response.json(response);
    });
    const pending = new PendingPrice();
    pending.setOwner(owner.user.id);
    const submitted = { ...quote };
    const first = pending.start(owner, submitted);
    submitted.price = "999";
    submitted.expected_version = 8;
    await first;
    assert.equal(pending.getSnapshot().status, "unknown");
    assert.equal(pending.dismiss(), false);

    await pending.retry(owner);
    assert.equal(pending.getSnapshot().status, "auth-required");
    assert.equal(pending.getSnapshot().plan, null);
    pending.setOwner(fresh.user.id);
    await pending.retry(fresh);
    assert.equal(pending.getSnapshot().status, "confirmed");
    assert.equal(pending.getSnapshot().record.version, 2);
    assert.deepEqual(
        requests.map((request) => JSON.parse(request.body)),
        [quote, quote, quote],
    );
    assert.equal(requests.at(-1).headers["X-CSRF-Token"], fresh.csrf_token);
});

test("a response for another price slot remains unconfirmed", async (t) => {
    t.mock.method(globalThis, "fetch", async () =>
        Response.json({ ...response, price_date: "2026-07-02" }),
    );
    const pending = new PendingPrice();
    pending.setOwner(owner.user.id);
    await pending.start(owner, quote);
    assert.equal(pending.getSnapshot().status, "unknown");
    assert.equal(pending.getSnapshot().record, null);
    pending.setOwner("fictional-second-owner");
    assert.equal(pending.getSnapshot().status, "idle");
    assert.equal(pending.getSnapshot().plan, null);
});
