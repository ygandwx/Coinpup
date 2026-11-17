import assert from "node:assert/strict";
import { test } from "node:test";
import { id, setupReturn, response } from "./return-revision-fixture.mjs";
const { validFrozenReturnRevision } = await import("../src/allocation-return-revision-receipt.ts");

const freeze = (data, cmd) => ({
    ledgerId: data.ledger,
    ownerId: id(9),
    returnId: data.group,
    key: id(10),
    action: cmd.action,
    bodyJson: JSON.stringify(cmd.body),
    reviewedJson: JSON.stringify(data.review),
});
test("receipt binds 400 ordered members to all 200 substitutions and original journals, including BIGINT source positions", () => {
    const d = setupReturn(200),
        cmd = d.correct;
    cmd.body.replacement.lines = d.sources.slice(200).map(d.line).reverse();
    const result = response(d, cmd),
        plan = freeze(d, cmd);
    assert.ok(validFrozenReturnRevision(result, plan));
    assert.equal(result.members.length, 400);
    for (const alter of [
        (r) => r.members.splice(199, 1),
        (r) => r.members.splice(399, 1),
        (r) => r.members.reverse(),
        (r) => {
            r.members[17].source_operation_id = id(75001);
        },
        (r) => {
            r.members[30].replacement.document_amount = "0.00000008";
        },
        (r) => {
            r.members[31].replacement.payment_amount = "0.10";
        },
        (r) => {
            r.members[40].document_line_id = id(75002);
        },
        (r) => {
            r.members[250].previous.journal_id = id(75003);
        },
        (r) => {
            r.members[251].operation_id = id(75004);
        },
        (r) => {
            r.members[252].source_return_no = "9007199254740992";
        },
        (r) => {
            r.members[253].previous.payment_amount = "0.04";
        },
        (r) => {
            r.members[0].operation_id = d.review.original.lines[10].operation_id;
        },
        (r) => {
            r.members[0].replacement.journal_id = d.review.original.lines[10].journal_id;
        },
        (r) => {
            r.payment_id = id(75005);
        },
        (r) => {
            r.previous.reason = "Other";
        },
        (r) => {
            r.replacement.reason = "Other";
        },
        (r) => {
            r.replacement.return_date = "2026-01-04";
        },
        (r) => {
            r.revision_id = null;
        },
        (r) => {
            r.version = 3;
        },
    ]) {
        const r = structuredClone(result);
        alter(r);
        assert.equal(validFrozenReturnRevision(r, plan), false);
    }
});
test("retained/removed/new mixed group uses replacement order then original order, while cancellation retains every prior member", () => {
    const d = setupReturn(3),
        cmd = d.correct;
    cmd.body.replacement.lines = [d.line(d.sources[2]), d.line(d.sources[3])];
    const r = response(d, cmd),
        p = freeze(d, cmd);
    assert.ok(validFrozenReturnRevision(r, p));
    assert.deepEqual(
        r.members.map((m) => m.action),
        ["correct", "create", "cancel", "cancel"],
    );
    const bad = structuredClone(r);
    bad.members[0].operation_id = id(77777);
    assert.equal(validFrozenReturnRevision(bad, p), false);
    const terminal = response(d, d.cancel),
        pc = freeze(d, d.cancel);
    assert.ok(validFrozenReturnRevision(terminal, pc));
    assert.equal(validFrozenReturnRevision(terminal, p), false);
    const before = JSON.stringify(pc);
    terminal.members[2].previous.document_amount = "0.00000001";
    assert.equal(validFrozenReturnRevision(terminal, pc), false);
    assert.equal(JSON.stringify(pc), before);
});
test("a returning original source needs a later slot, while a first-time source can use slot one", () => {
    const d = setupReturn(2);
    const current = d.review.current;
    current.members.pop();
    current.members[0].operation_version = 2;
    current.members[0].journal_id = id(75021);
    current.version = d.correct.body.expected_version = 2;
    current.revision_id = id(75022);
    current.revision_reason = "Fictional removal";
    const rejoining = d.line(d.sources[1]);
    rejoining.payment_amount = "0.04";
    rejoining.document_amount = "0.00000004";
    d.correct.body.replacement.lines = [rejoining, d.line(d.sources[2])];
    const plan = freeze(d, d.correct);
    const receipt = response(d, d.correct);
    receipt.members[0].source_return_no = "2";
    receipt.members[1].source_return_no = "1";
    assert.ok(validFrozenReturnRevision(receipt, plan));
    receipt.members[0].source_return_no = "1";
    assert.equal(validFrozenReturnRevision(receipt, plan), false);
});
