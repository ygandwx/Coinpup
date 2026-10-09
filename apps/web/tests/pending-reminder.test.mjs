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
const { scopedReminder, matchesReminder } = await import("../src/reminder-intent.ts");
const { PendingReminderController } = await import("../src/pending-reminder.ts");
const { ApiError } = await import("../src/api.ts");
const api = await import("../src/reminder-api.ts");
const request = {
    id: "fictional-event",
    title: " Fictional deadline ",
    event_kind: "certificate",
    rule: {
        rule_id: "certificate.expiry",
        rule_version: "R-v1",
        expiry_date: "2027-01-31",
        applicability_confirmed: true,
    },
};
const plan = {
    id: request.id,
    ownerId: "fictional-owner",
    ledgerId: "fictional-ledger",
    action: "create",
    bodyJson: JSON.stringify(request),
};
const row = {
    id: plan.id,
    ledger_id: plan.ledgerId,
    last_actor_id: plan.ownerId,
    version: 1,
    event_kind: "certificate",
    title: "Fictional deadline",
    notes: null,
    manual_reason: null,
    last_reason: null,
    completed: false,
    archived: false,
    evaluation_status: "calculated",
    last_action: "create",
    calculated_date: "2027-01-31",
    manual_due_date: null,
    effective_date: "2027-01-31",
    created_at: "2026-10-10T00:00:00Z",
    updated_at: "2026-10-10T00:00:00Z",
    evaluation: {
        status: "calculated",
        calculated_date: "2027-01-31",
        inputs: {
            filing_year: null,
            period_end: null,
            expiry_date: "2027-01-31",
            applicability_confirmed: true,
        },
        rule: {
            id: "certificate.expiry",
            version: "R-v1",
            checked_on: "2026-10-10",
            sources: ["Fictional test source"],
            required: ["expiry_date"],
        },
        reasons: [],
        missing: [],
    },
};
test("creation requires exact scope, rule version and original input", () => {
    assert.equal(scopedReminder(row, plan), true);
    assert.equal(matchesReminder(row, plan), true);
    for (const patch of [
        { id: "foreign" },
        { ledger_id: "foreign" },
        { last_actor_id: "foreign" },
        { version: 0 },
        { version: true },
        { effective_date: null },
        { calculated_date: "2027-02-30" },
        { completed: "false" },
    ])
        assert.equal(scopedReminder({ ...row, ...patch }, plan), false);
    for (const patch of [
        { version: 2 },
        { title: "Other" },
        { event_kind: "tax" },
        { completed: true },
        { archived: true },
    ])
        assert.equal(matchesReminder({ ...row, ...patch }, plan), false);
    const changed = structuredClone(row);
    changed.evaluation.rule.version = "R-v2";
    assert.equal(matchesReminder(changed, plan), false);
    changed.evaluation.rule.version = "R-v1";
    changed.evaluation.inputs.expiry_date = "2027-02-01";
    assert.equal(matchesReminder(changed, plan), false);
});
test("manual dates require exact reason and consistent effective date", () => {
    const body = {
        expected_version: 1,
        manual_due_date: "2027-02-10",
        reason: " Fictional notice ",
    };
    const intent = { ...plan, action: "manual", bodyJson: JSON.stringify(body) };
    const reply = {
        ...row,
        version: 2,
        last_action: "set_manual",
        manual_due_date: body.manual_due_date,
        manual_reason: "Fictional notice",
        last_reason: "Fictional notice",
        effective_date: body.manual_due_date,
    };
    assert.equal(scopedReminder(reply, intent), true);
    assert.equal(matchesReminder(reply, intent), true);
    assert.equal(scopedReminder({ ...reply, effective_date: row.calculated_date }, intent), false);
    assert.equal(matchesReminder({ ...reply, last_reason: "Different" }, intent), false);
});
test("manual-only creation never invents a calculated deadline", () => {
    const body = {
        id: plan.id,
        title: row.title,
        event_kind: "tax",
        manual_due_date: "2027-02-01",
        manual_reason: "Fictional tax notice",
    };
    const intent = { ...plan, bodyJson: JSON.stringify(body) };
    const reply = {
        ...row,
        event_kind: "tax",
        evaluation: null,
        evaluation_status: "missing_parameters",
        calculated_date: null,
        manual_due_date: body.manual_due_date,
        manual_reason: body.manual_reason,
        effective_date: body.manual_due_date,
    };
    assert.equal(scopedReminder(reply, intent), true);
    assert.equal(matchesReminder(reply, intent), true);
});

const session = { user: { id: plan.ownerId }, csrf_token: "fictional-token" };
test("unknown creation freezes the request through duplicate clicks and reauthentication", async () => {
    const calls = [];
    const controller = new PendingReminderController(async (s, intent) => {
        calls.push({ body: intent.bodyJson, csrf: s.csrf_token });
        if (calls.length === 1) throw new ApiError("network");
        if (calls.length === 2) throw new ApiError("unauthorized", 401);
        return structuredClone(row);
    });
    controller.setOwner(plan.ownerId);
    const input = structuredClone(request);
    const flight = controller.start(session, plan.ledgerId, plan.id, "create", input);
    input.rule.expiry_date = "2099-01-01";
    await assert.rejects(controller.start(session, plan.ledgerId, plan.id, "create", input));
    await flight;
    assert.equal(controller.getSnapshot().status, "unknown");
    assert.equal(controller.dismiss(), false);
    await controller.retry(session);
    assert.equal(controller.getSnapshot().status, "auth-required");
    assert.equal(controller.getSnapshot().plan, null);
    controller.setOwner(null);
    controller.setOwner(plan.ownerId);
    await controller.retry({ ...session, csrf_token: "fictional-fresh" });
    assert.equal(controller.getSnapshot().status, "confirmed");
    assert.equal(new Set(calls.map((x) => x.body)).size, 1);
    assert.equal(calls[2].csrf, "fictional-fresh");
});

test("unknown updates keep old version even when current fields match", async () => {
    let count = 0;
    const controller = new PendingReminderController(
        async () => {
            if (++count === 1) throw new ApiError("network");
            throw new ApiError("server", 409, "version_conflict");
        },
        async () => ({ ...row, version: 2, last_action: "edit" }),
    );
    controller.setOwner(plan.ownerId);
    await controller.start(session, plan.ledgerId, plan.id, "save", {
        expected_version: 1,
        title: row.title,
        notes: null,
    });
    await controller.retry(session);
    assert.equal(controller.getSnapshot().status, "conflict");
    assert.equal(JSON.parse(controller.getSnapshot().plan.bodyJson).expected_version, 1);
});

test("late responses cannot resurrect state after explicit logout", async () => {
    let resolve;
    const response = new Promise((done) => {
        resolve = done;
    });
    const controller = new PendingReminderController(async () => response);
    controller.setOwner(plan.ownerId);
    const flight = controller.start(session, plan.ledgerId, plan.id, "create", request);
    await Promise.resolve();
    controller.setOwner(null, true);
    resolve(row);
    await flight;
    assert.equal(controller.getSnapshot().status, "idle");
    assert.equal(controller.getSnapshot().record, null);
});

test("duplicate creation requires an exact initial record and failed reads remain unknown", async () => {
    for (const outcome of ["same", "edited", "different", "foreign", "read-failed"]) {
        const controller = new PendingReminderController(
            async () => {
                throw new ApiError("server", 409, "duplicate_record");
            },
            async () => {
                if (outcome === "read-failed") throw new ApiError("network");
                const value = structuredClone(row);
                if (outcome === "edited") value.version++;
                if (outcome === "different") value.evaluation.rule.version = "R-v2";
                if (outcome === "foreign") value.ledger_id = "other";
                return value;
            },
        );
        controller.setOwner(plan.ownerId);
        await controller.start(session, plan.ledgerId, plan.id, "create", request);
        assert.equal(
            controller.getSnapshot().status,
            outcome === "same"
                ? "confirmed"
                : ["edited", "different"].includes(outcome)
                  ? "conflict"
                  : "unknown",
            outcome,
        );
    }
});

test("every explicit transition and recalculation must match its direct response", () => {
    for (const action of ["complete", "reopen", "archive", "restore"]) {
        const intent = {
            ...plan,
            action: "transition",
            bodyJson: JSON.stringify({ expected_version: 1, action }),
        };
        const result = {
            ...row,
            version: 2,
            last_action: action,
            completed: action === "complete",
            archived: action === "archive",
        };
        assert.equal(matchesReminder(result, intent), true);
        assert.equal(matchesReminder({ ...result, last_action: "edit" }, intent), false);
    }
    const intent = {
        ...plan,
        action: "recalculate",
        bodyJson: JSON.stringify({ expected_version: 1, rule: request.rule }),
    };
    assert.equal(matchesReminder({ ...row, version: 2, last_action: "recalculate" }, intent), true);
    assert.equal(matchesReminder({ ...row, version: 2, last_action: "edit" }, intent), false);
});

test("transport encodes identities and sends current CSRF only for writes", async () => {
    const original = globalThis.fetch,
        calls = [];
    globalThis.fetch = async (url, options) => {
        calls.push({ url, ...options });
        return new Response("{}", { headers: { "Content-Type": "application/json" } });
    };
    try {
        for (const action of ["create", "save", "recalculate", "manual", "transition"])
            await api.saveReminder(
                "fictional-current",
                "fictional/ledger",
                "fictional/event",
                action,
                request,
            );
        await api.getReminder("fictional/ledger", "fictional/event");
        await api.listReminders("fictional/ledger", 25);
        await api.listReminderRules("fictional/ledger");
        await api.listReminderRevisions("fictional/ledger", "fictional/event", 50);
        assert.deepEqual(
            calls.slice(0, 5).map((call) => call.method),
            ["POST", "PATCH", "POST", "PATCH", "POST"],
        );
        assert.ok(calls.every((call) => call.url.includes("fictional%2Fledger")));
        assert.ok(calls[2].url.endsWith("fictional%2Fevent/recalculate"));
        assert.ok(calls[3].url.endsWith("fictional%2Fevent/manual-date"));
        assert.ok(calls[4].url.endsWith("fictional%2Fevent/transition"));
        assert.ok(calls[6].url.includes("offset=25"));
        assert.ok(calls[8].url.endsWith("revisions?limit=25&offset=50"));
        assert.ok(
            calls
                .slice(0, 5)
                .every(
                    (call) => new Headers(call.headers).get("X-CSRF-Token") === "fictional-current",
                ),
        );
        assert.ok(calls.slice(5).every((call) => !new Headers(call.headers).has("X-CSRF-Token")));
    } finally {
        globalThis.fetch = original;
    }
});
