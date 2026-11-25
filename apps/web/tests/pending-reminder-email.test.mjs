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

const { PendingReminderEmail } = await import("../src/pending-reminder-email.ts");

test("reminder mail retries the frozen versions after login and treats a changed event as a conflict", async (t) => {
    const setup = {
        expected_event_version: 3,
        expected_config_version: 2,
        enabled: true,
        lead_days: 1,
        timezone: "UTC",
        minute: 30,
        locale: "en",
        inbox_enabled: true,
        email_enabled: true,
        email_recipient: "fictional@example.invalid",
    };
    const requests = [];
    t.mock.method(globalThis, "fetch", async (_route, options) => {
        if (options.method === "PUT") {
            requests.push(options);
            if (requests.length === 1) throw new Error("Connection removed");
            if (requests.length === 2) return Response.json(null, { status: 401 });
            return Response.json({ detail: { code: "version_conflict" } }, { status: 409 });
        }
        return Response.json({
            ledger_id: "ledger",
            event_id: "event",
            event_version: 4,
            config: { version: 2 },
        });
    });

    const session = { user: { id: "fictional-owner" }, csrf_token: "fictional-before" };
    const fresh = { ...session, csrf_token: "fictional-after" };
    const pending = new PendingReminderEmail();
    pending.setOwner(session.user.id);
    const mutable = { ...setup };
    const saving = pending.start(session, "ledger", "event", mutable);
    mutable.email_recipient = "other-fictional@example.invalid";
    await saving;
    assert.equal(pending.getSnapshot().status, "unknown");
    assert.equal(pending.dismiss(), false);
    await pending.retry(session);
    assert.equal(pending.getSnapshot().status, "auth-required");
    assert.equal(pending.getSnapshot().plan, null);

    pending.setOwner(fresh.user.id);
    await pending.retry(fresh);
    assert.deepEqual(
        requests.map((request) => JSON.parse(request.body)),
        [setup, setup, setup],
    );
    assert.equal(requests.at(-1).headers["X-CSRF-Token"], fresh.csrf_token);
    assert.equal(pending.getSnapshot().status, "conflict");
    assert.equal(pending.getSnapshot().record.event_version, 4);
});
