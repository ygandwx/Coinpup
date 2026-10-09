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
const { reminderHistoryRows } = await import("../src/reminder-history.ts");
const { reminderError } = await import("../src/reminder-errors.ts");
const { ApiError } = await import("../src/api.ts");
const snapshot = {
    id: "fictional-event",
    ledger_id: "fictional-ledger",
    last_actor_id: "fictional-owner",
    version: 1,
    event_kind: "tax",
    title: "Fictional date",
    notes: null,
    manual_reason: "Fictional reason",
    last_reason: null,
    completed: false,
    archived: false,
    evaluation_status: "missing_parameters",
    last_action: "create",
    calculated_date: null,
    manual_due_date: "2028-01-31",
    evaluation: null,
    created_at: "2026-10-10T00:00:00Z",
    updated_at: "2026-10-10T00:00:00Z",
};
const revision = {
    id: "fictional-revision",
    ledger_id: snapshot.ledger_id,
    event_id: snapshot.id,
    version: 1,
    snapshot,
    created_at: snapshot.created_at,
};
const valid = (rows, offset = 0) =>
    reminderHistoryRows(rows, "fictional-owner", "fictional-ledger", "fictional-event", offset);
test("history accepts immutable saved dates without a response-only effective date", () => {
    const before = structuredClone(revision);
    assert.equal(valid([revision]), true);
    assert.deepEqual(revision, before);
    assert.equal("effective_date" in revision.snapshot, false);
    assert.equal(valid([]), true);
    const next = { ...revision, version: 26, snapshot: { ...snapshot, version: 26 } };
    assert.equal(valid([next], 25), true);
    assert.equal(valid([next], 0), false);
});
test("history rejects foreign scope, gaps, malformed dates and forged snapshot versions", () => {
    for (const patch of [
        { ledger_id: "foreign" },
        { event_id: "foreign" },
        { version: 2 },
        { id: null },
        { created_at: "not-a-timestamp" },
        { snapshot: null },
        { snapshot: [] },
        ...[
            { id: "foreign" },
            { ledger_id: "foreign" },
            { last_actor_id: "foreign" },
            { version: 2 },
            { completed: "false" },
            { manual_due_date: "2028-02-30" },
            { manual_reason: null },
            { evaluation_status: "calculated", calculated_date: null },
        ].map((value) => ({ snapshot: { ...snapshot, ...value } })),
    ])
        assert.equal(valid([{ ...revision, ...patch }]), false, JSON.stringify(patch));
    assert.equal(valid([revision, revision]), false);
    assert.equal(valid([revision], -1), false);
    assert.equal(valid([revision], 0.5), false);
    assert.equal(valid({}), false);
    assert.equal(valid(Array(26).fill(revision)), false);
});
test("reminder errors provide bilingual recovery guidance without leaking response text", () => {
    for (const code of [
        "reminder_date",
        "reminder_title",
        "reminder_notes",
        "reminder_filing_year",
        "reminder_manual_reason",
        "reminder_rule_required",
        "reminder_rule_version_unknown",
        "reminder_rule_kind",
        "version_conflict",
    ]) {
        const error = new ApiError("server", 422, code);
        const zh = reminderError(error, "zh"),
            en = reminderError(error, "en");
        assert.notEqual(zh, en);
        assert.match(zh, /[\u4e00-\u9fff]/u);
        assert.doesNotMatch(en, /[\u4e00-\u9fff]/u);
        assert.equal(en.includes(code), false);
    }
    assert.match(reminderError(new ApiError("network"), "en"), /recovery/);
    assert.equal(
        reminderError(new Error("Fictional private data"), "en").includes("Fictional private data"),
        false,
    );
});
