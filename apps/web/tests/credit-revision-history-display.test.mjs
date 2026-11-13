import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { test } from "node:test";
import ts from "typescript";
import { renderToStaticMarkup } from "react-dom/server";
import { currentRow, id, ledger, creditId, documentId } from "./credit-revision-fixture.mjs";
registerHooks({
    resolve(s, c, next) {
        return next(
            s.startsWith(".") && !/\.[a-z]+$/u.test(s) && c.parentURL?.includes("/src/")
                ? `${s}.ts`
                : s,
            c,
        );
    },
    load(url, c, next) {
        if (url.endsWith(".css")) return { format: "module", shortCircuit: true, source: "" };
        if (url.endsWith(".tsx"))
            return {
                format: "module",
                shortCircuit: true,
                source: ts.transpileModule(readFileSync(new URL(url), "utf8"), {
                    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext },
                }).outputText,
            };
        return next(url, c);
    },
});
const { BusinessCreditRevisionHistory: View } =
    await import("../src/BusinessCreditRevisionHistory.tsx");
const { creditHistoryDisplay: display } = await import("../src/credit-revision-history-display.ts");
const { ApiError } = await import("../src/api.ts");
const scope = { ledgerId: ledger, creditId, documentId, assetId: "USD" };
const entry = (version, action = "correct") => ({
    version,
    action: version === 1 ? "create" : action,
    revision_id: version === 1 ? null : id(800 + version),
    reason: "Fictional 原因 <script>",
    created_at: "2026-11-07T01:00:00Z",
});
function revision(action = "correct") {
    const member = (n, type, position) => {
        const old = type !== "create",
            added = type !== "cancel",
            source = currentRow(n);
        return {
            action: type,
            source_line_id: source.source_line_id,
            source_operation_id: source.source_operation_id,
            operation_id: source.operation_id,
            operation_version: old ? source.operation_version + 1 : 1,
            previous_line_no: old ? n : null,
            previous_operation_version: old ? source.operation_version : null,
            previous_journal_id: old ? source.journal_id : null,
            reversal_journal_id: old ? id(600 + n) : null,
            previous_net_amount: old ? source.net_amount : null,
            previous_tax_amount: old ? source.tax_amount : null,
            previous_total_amount: old ? source.total_amount : null,
            previous_recognition_date: old ? source.recognition_date : null,
            line_no: added ? position : null,
            journal_id: added ? id(700 + n) : null,
            net_amount: added ? "1.000000000000000000" : null,
            tax_amount: added ? "0.000000000000000000" : null,
            total_amount: added ? "1.000000000000000000" : null,
            recognition_date: added ? "2026-11-07" : null,
        };
    };
    return {
        kind: "business_credit_revision",
        credit_id: creditId,
        ledger_id: ledger,
        document_id: documentId,
        asset_id: "USD",
        revision_id: entry(4).revision_id,
        version: 4,
        previous_version: 3,
        action,
        reason: entry(4).reason,
        previous_credit_date: "2026-11-05",
        previous_credit_reason: "Fictional prior 中文",
        credit_date: action === "correct" ? "2026-11-07" : null,
        replacement_reason: action === "correct" ? "Fictional replacement" : null,
        members:
            action === "correct"
                ? [member(3, "create", 1), member(1, "correct", 2), member(2, "cancel", null)]
                : [member(1, "cancel", null), member(2, "cancel", null)],
    };
}
function original() {
    const line = currentRow(1);
    delete line.operation_version;
    const common = {
        id: creditId,
        ledger_id: ledger,
        document_id: documentId,
        asset_id: "USD",
        source_version: 2,
        credit_date: "2026-11-01",
    };
    return {
        ...common,
        document_kind: "bill",
        party_id: id(7),
        created_at: entry(1).created_at,
        reason: entry(1).reason,
        receipt: { ...common, kind: "business_credit", postings: [line] },
    };
}
function props(value = [entry(1), entry(2)], selected = null) {
    const calls = [];
    return {
        ...scope,
        locale: "en",
        available: true,
        requestKey: "owner/session/ledger/credit/page/refresh",
        offset: 0,
        selected,
        loaded: { key: "owner/session/ledger/credit/page/refresh", value },
        failure: null,
        calls,
        onSelect: (v) => calls.push(v),
        onPage: (v) => calls.push(v),
        onReload: () => calls.push("reload"),
    };
}
const html = (p) => renderToStaticMarkup(View(p));
function buttons(node) {
    if (Array.isArray(node)) return node.flatMap(buttons);
    if (!node || typeof node !== "object") return [];
    return [...(node.type === "button" ? [node] : []), ...buttons(node.props?.children)];
}
test("metadata pages retain original/create IDs, pagination bounds and actual callbacks in both languages", () => {
    const p = props(Array.from({ length: 25 }, (_, n) => entry(n + 1)));
    for (const locale of ["zh", "en"]) {
        p.locale = locale;
        assert.equal((html(p).match(/<li\b/gu) ?? []).length, 25);
        assert.ok(html(p).includes(locale === "zh" ? "原创建" : "Original creation"));
    }
    const b = buttons(View(p));
    b[0].props.onClick();
    b[26].props.onClick();
    b[27].props.onClick();
    assert.deepEqual(p.calls, [entry(1), 25, "reload"]);
    assert.equal(b[25].props.disabled, true);
    p.offset = 100000;
    p.loaded.value = Array.from({ length: 25 }, (_, n) => entry(n + 100001));
    const last = buttons(View(p));
    assert.equal(last[26].props.disabled, true);
    last[25].props.onClick();
    assert.equal(p.calls.at(-1), 99975);
    p.loaded.value = [];
    assert.match(html(p), /No versions/);
    assert.equal(buttons(View(p))[1].props.disabled, true);
});
test("scope/request generations hide old successes, failures and unauthorized private content", () => {
    const p = props();
    for (const changed of ["owner", "ledger", "credit", "page", "revision", "refresh"]) {
        p.requestKey = changed;
        p.failure = { key: changed + "-old", error: new ApiError("server", 401, "unauthorized") };
        assert.match(html(p), /Loading/);
        assert.doesNotMatch(html(p), /Fictional|Sign in/);
    }
    p.requestKey = p.loaded.key;
    p.failure = { key: p.requestKey, error: new ApiError("server", 401, "unauthorized") };
    assert.match(html(p), /Sign in/);
    assert.doesNotMatch(html(p), /Fictional/);
    assert.equal(buttons(View(p)).length, 0);
    p.failure = null;
    p.available = false;
    assert.doesNotMatch(html(p), /Fictional/);
});
test("creation 201 and fixed correct/cancel 200 render separately without requiring today's current", () => {
    const cases = [
        [original(), entry(1)],
        [revision(), entry(4)],
        [revision("cancel"), entry(4, "cancel")],
    ];
    for (const [value, selected] of cases)
        for (const locale of ["en", "zh"]) {
            const p = { ...props(value, selected), locale },
                saved = JSON.stringify(value);
            assert.equal(display(value, scope, 0, selected)?.kind, "detail");
            assert.ok(
                html(p).includes(
                    locale === "zh" ? "不能确认未知提交" : "proof of an unknown submission",
                ),
            );
            assert.match(html(p), /&lt;script&gt;/);
            assert.doesNotMatch(html(p), /<script>/);
            assert.equal(JSON.stringify(value), saved);
            const b = buttons(View(p));
            assert.equal(b.length, 2);
            b[1].props.onClick();
            assert.deepEqual(p.calls, [null]);
        }
    const p = props(revision(), entry(4)),
        rendered = html(p);
    assert.ok(rendered.indexOf("Added member") < rendered.indexOf("Retained and corrected"));
    assert.ok(rendered.indexOf("Retained and corrected") < rendered.indexOf("Removed member"));
    assert.match(rendered, /Member operation version: 1/);
    assert.match(rendered, /Group version: 4/);
    assert.match(rendered, /1\.000000000000000000/);
    assert.match(html(props(revision("cancel"), entry(4, "cancel"))), /no zero-amount replacement/);
});
test("18-place minimum and maximum remain exact and no current precision or pending plan is needed", () => {
    for (const amount of ["0.000000000000000001", "99999999999999999999.999999999999999999"]) {
        const value = revision();
        value.asset_id = "ETH";
        Object.assign(value.members[0], { net_amount: amount, total_amount: amount });
        const p = { ...props(value, entry(4)), assetId: "ETH" };
        assert.equal(display(value, p, 0, p.selected)?.kind, "detail");
        assert.ok(html(p).includes(amount.split(".")[1]));
    }
});
test("invalid page/detail identity, malformed values and inconsistent nullable transitions fail safely", () => {
    for (const mutate of [
        (v) => (v.ledger_id = id(9)),
        (v) => (v.credit_id = id(9)),
        (v) => (v.document_id = id(9)),
        (v) => (v.asset_id = "EUR"),
        (v) => (v.revision_id = id(9)),
        (v) => v.version++,
        (v) => (v.action = "cancel"),
        (v) => (v.members[0].net_amount = "-0"),
        (v) => (v.members[0].tax_amount = "2"),
        (v) => (v.members[2].journal_id = id(9)),
        (v) => (v.members[0].previous_net_amount = "0"),
        (v) => v.members[1].operation_version++,
        (v) => (v.members[1].previous_line_no = 2),
        (v) => v.members.reverse(),
    ]) {
        const value = revision();
        mutate(value);
        const p = props(value, entry(4));
        assert.equal(display(value, scope, 0, p.selected), null);
        assert.match(html(p), /could not be read safely/);
        assert.doesNotMatch(html(p), /Fictional|Member operation version/);
    }
    for (const value of [
        [entry(2)],
        [entry(1), entry(1)],
        [{ ...entry(1), revision_id: id(9) }],
        null,
    ])
        assert.equal(display(value, scope, 0, null), null);
    const value = original();
    value.receipt.document_id = id(9);
    assert.equal(display(value, scope, 0, entry(1)), null);
});
test("read failures expose no raw error and read-only callbacks cannot discard a pending command", () => {
    const p = props(revision(), entry(4));
    p.failure = { key: p.requestKey, error: new Error("PRIVATE ERROR") };
    assert.doesNotMatch(html(p), /PRIVATE ERROR|Fictional/);
    assert.match(html(p), /does not retry or change/);
    const b = buttons(View(p));
    assert.equal(b.length, 2);
    b[0].props.onClick();
    b[1].props.onClick();
    assert.deepEqual(p.calls, ["reload", null]);
});
