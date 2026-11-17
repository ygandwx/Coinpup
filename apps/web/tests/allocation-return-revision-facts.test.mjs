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
const { validReturnCurrent, validReturnRevision } =
    await import("../src/allocation-return-revision-facts.ts");
const { validHistoryPage } = await import("../src/settlement-revision-facts.ts");
const id = (n) => "20000000-0000-4000-9000-" + String(n).padStart(12, "0");
const scope = { ledger_id: id(1), return_id: id(2), payment_id: id(3), payment_asset_id: "USD" };
const assets = [
    { asset_id: "USD", scale: 2 },
    { asset_id: "BTC", scale: 8 },
];
const day = { reason: "Fictional return 中文", return_date: "2026-10-01" };
const identity = (n) => ({
    source_allocation_line_id: id(1000 + n),
    source_operation_id: id(2000 + n),
    source_version: 1,
    source_return_no: "9223372036854775807",
    operation_id: id(3000 + n),
    operation_version: 2,
    document_id: id(4),
    document_line_id: id(4000 + n),
    document_asset_id: "BTC",
});
const fact = (n, v, j) => ({
    line_no: n,
    operation_version: v,
    journal_id: id(j),
    payment_amount: "0.030000000000000000",
    document_amount: "0.000000010000000000",
});
const header = {
    ...scope,
    party_id: id(5),
    payment_direction: "payout",
    version: 2,
    status: "active",
    revision_id: id(6),
    revision_reason: "Fictional correction",
    fact: day,
};
test("current returns require one ordered scoped group and the actual precision of each side", () => {
    const members = Array.from({ length: 200 }, (_, i) => ({
        ...identity(i),
        ...fact(i + 1, 2, 5000 + i),
        project_id: null,
    }));
    const c = { ...header, members };
    assert.ok(validReturnCurrent(c, scope, assets));
    for (const bad of [
        { ledger_id: id(9) },
        { revision_reason: null },
        { members: [] },
        { members: [...members, members[0]] },
        { members: [{ ...members[0], source_version: 2 }] },
        { members: [{ ...members[0], source_return_no: "9223372036854775808" }] },
        { members: [{ ...members[0], payment_amount: "0.001" }] },
        { members: [{ ...members[0], document_asset_id: "USD" }] },
        { members: [members[0], { ...members[1], source_operation_id: members[0].operation_id }] },
        { members: [{ ...members[0], operation_version: 3 }] },
        { members: [{ ...members[0], project_id: "other" }] },
    ])
        assert.equal(validReturnCurrent({ ...c, ...bad }, scope, assets), false);
    assert.ok(
        validReturnCurrent({ ...c, status: "cancelled", fact: null, members: [] }, scope, assets),
    );
    assert.equal(validReturnCurrent({ ...c, status: "cancelled" }, scope, assets), false);
});
test("return directions accept only the two literal wire strings for active and cancelled groups", () => {
    const member = { ...identity(0), ...fact(1, 2, 5000), project_id: null };
    const active = { ...header, members: [member] };
    const cancelled = { ...header, status: "cancelled", fact: null, members: [] };
    for (const current of [active, cancelled]) {
        const valid = (direction) =>
            validReturnCurrent({ ...current, payment_direction: direction }, scope, assets);
        assert.ok(valid("payout"));
        assert.ok(valid("receipt"));
        for (const direction of [["receipt"], ["payout"], { toString: null }, null])
            assert.equal(valid(direction), false);
    }
});
test("400 union members prove all 200 replacements and the previous complete retired group", () => {
    const members = [];
    for (let i = 0; i < 200; i++)
        members.push({
            ...identity(i),
            operation_version: 1,
            action: "create",
            previous: null,
            reversal_journal_id: null,
            replacement: fact(i + 1, 1, 5000 + i),
        });
    for (let i = 0; i < 200; i++)
        members.push({
            ...identity(i + 200),
            action: "cancel",
            previous: fact(i + 1, 1, 6000 + i),
            reversal_journal_id: id(7000 + i),
            replacement: null,
        });
    const row = {
        ...scope,
        kind: "business_allocation_return_revision",
        revision_id: id(8),
        previous_version: 1,
        version: 2,
        action: "correct",
        reason: "Fictional moved sources",
        previous: day,
        replacement: day,
        members,
    };
    const entry = { version: 2, revision_id: id(8), action: "correct", reason: row.reason };
    const valid = (changed, e = entry) => validReturnRevision(changed, scope, e, assets);
    assert.ok(valid(row));
    assert.equal(valid(row, { ...entry, revision_id: id(99) }), false);
    assert.equal(
        valid({ ...row, action: "cancel", replacement: null }, { ...entry, action: "cancel" }),
        false,
    );
    const badMember = (at, delta) => {
        const values = [...members];
        values[at] = { ...values[at], ...delta };
        return { ...row, members: values };
    };
    for (const value of [
        badMember(100, { operation_id: members[200].operation_id }),
        badMember(11, { replacement: fact(11, 1, 8000) }),
        badMember(210, { reversal_journal_id: members[0].replacement.journal_id }),
        badMember(211, { source_operation_id: members[0].operation_id }),
        badMember(0, { operation_version: 2 }),
        badMember(210, { previous: fact(10, 1, 9000) }),
        badMember(210, { replacement: fact(1, 1, 9010) }),
        badMember(97, { source_return_no: 9007199254740992 }),
        badMember(202, { previous: { ...members[202].previous, document_amount: "0.000000001" } }),
        {
            ...row,
            members: [...members.slice(0, 199), members[200], members[199], ...members.slice(201)],
        },
    ])
        assert.equal(valid(value), false);
    const first = members[0],
        previous = fact(1, 1, 9100);
    const retained = {
        ...first,
        action: "correct",
        operation_version: 2,
        previous,
        reversal_journal_id: id(9101),
        replacement: fact(1, 2, 9102),
    };
    assert.ok(valid({ ...row, members: [retained] }));
    assert.ok(
        valid(
            {
                ...row,
                action: "cancel",
                replacement: null,
                members: [{ ...retained, action: "cancel", replacement: null }],
            },
            { ...entry, action: "cancel" },
        ),
    );
});
test("shared directory preserves original and terminal semantics across page boundaries", () => {
    const stamp = "2026-10-10T00:00:00Z";
    const first = {
        version: 1,
        revision_id: null,
        action: "create",
        reason: "Fictional creation",
        created_at: stamp,
    };
    const second = {
        version: 2,
        revision_id: id(8),
        action: "cancel",
        reason: "Fictional cancel",
        created_at: stamp,
    };
    assert.ok(validHistoryPage([first, second], 0));
    assert.ok(validHistoryPage([second], 1));
    assert.equal(validHistoryPage([second], 0), false);
    assert.equal(
        validHistoryPage(
            [first, second, { ...second, revision_id: id(9), action: "correct", version: 3 }],
            0,
        ),
        false,
    );
});
