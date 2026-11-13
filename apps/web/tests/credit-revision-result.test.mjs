import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { test } from "node:test";
import ts from "typescript";
import { renderToStaticMarkup } from "react-dom/server";
import {
    fixture,
    cancelled,
    plan,
    id,
    ledger,
    creditId,
    documentId,
} from "./credit-revision-fixture.mjs";
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
    load(url, context, next) {
        if (url.endsWith(".css")) return { format: "module", shortCircuit: true, source: "" };
        if (url.endsWith(".tsx"))
            return {
                format: "module",
                shortCircuit: true,
                source: ts.transpileModule(readFileSync(new URL(url), "utf8"), {
                    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext },
                }).outputText,
            };
        return next(url, context);
    },
});
const { BusinessCreditRevisionResult } = await import("../src/BusinessCreditRevisionResult.tsx");
const { creditRevisionError } = await import("../src/credit-revision-errors.ts");
const { ApiError } = await import("../src/api.ts");
function props(f = fixture()) {
    const calls = [];
    return {
        plan: plan(f),
        status: "unknown",
        revisionReceipt: null,
        error: null,
        locale: "en",
        calls,
        onRetry() {
            calls.push("retry");
        },
        onEdit() {
            calls.push("edit");
        },
        onClose() {
            calls.push("close");
        },
    };
}
const markup = (p) => renderToStaticMarkup(BusinessCreditRevisionResult(p));
function buttons(node) {
    if (Array.isArray(node)) return node.flatMap(buttons);
    if (!node || typeof node !== "object") return [];
    return [...(node.type === "button" ? [node] : []), ...buttons(node.props?.children)];
}
function confirmed(action = "correct") {
    const f = action === "correct" ? fixture() : cancelled();
    f.review.current.members = [f.review.current.members[0]];
    if (action === "correct") f.body.replacement.lines = [f.body.replacement.lines[1]];
    const p = props(f);
    p.status = "confirmed";
    p.revisionReceipt = {
        kind: "business_credit_revision",
        credit_id: creditId,
        revision_id: id(8),
        ledger_id: ledger,
        document_id: documentId,
        asset_id: "USD",
        previous_version: 3,
        version: 4,
        action,
        reason: "Fictional correction 中文",
        previous_credit_date: "2026-11-05",
        previous_credit_reason: "Fictional current",
        credit_date: action === "correct" ? "2026-11-06" : null,
        replacement_reason: action === "correct" ? "Fictional replacement" : null,
        members: [
            {
                action: action === "correct" ? "correct" : "cancel",
                source_line_id: id(101),
                source_operation_id: id(201),
                operation_id: id(401),
                operation_version: 3,
                previous_line_no: 1,
                previous_operation_version: 2,
                previous_journal_id: id(501),
                reversal_journal_id: id(701),
                previous_net_amount: "17.230000000000000000",
                previous_tax_amount: "2.770000000000000000",
                previous_total_amount: "20.000000000000000000",
                previous_recognition_date: "2026-11-01",
                line_no: action === "correct" ? 1 : null,
                journal_id: action === "correct" ? id(601) : null,
                net_amount: action === "correct" ? "20.000000000000000000" : null,
                tax_amount: action === "correct" ? "3.000000000000000000" : null,
                total_amount: action === "correct" ? "23.000000000000000000" : null,
                recognition_date: action === "correct" ? "2026-11-03" : null,
            },
        ],
    };
    return p;
}

test("bilingual result keeps full C/A/B order, independent versions and user text without mutating intent", () => {
    const p = props(),
        original = p.plan.bodyJson + p.plan.reviewedJson;
    for (const locale of ["en", "zh"]) {
        p.locale = locale;
        const html = markup(p);
        assert.equal((html.match(/<li\b/gu) ?? []).length, 3);
        const labels =
            locale === "en"
                ? ["Added", "Retained and corrected", "Removed"]
                : ["新增", "保留并更正", "移除"];
        assert(
            html.indexOf(labels[0]) < html.indexOf(labels[1]) &&
                html.indexOf(labels[1]) < html.indexOf(labels[2]),
        );
        assert.match(html, /Fictional current/u);
        assert.match(html, /Fictional correction 中文/u);
        assert.match(html, /Fictional replacement/u);
        assert.match(html, /17\.23 USD/u);
        assert.match(html, /20\.00 USD/u);
        assert(
            html.includes(
                locale === "en" ? "Previous member operation version: 2" : "前版行操作版本: 2",
            ),
        );
        assert(
            html.includes(
                locale === "en" ? "Expected member operation version: 3" : "预期行操作版本: 3",
            ),
        );
        assert(
            html.includes(
                locale === "en" ? "Expected member operation version: 1" : "预期行操作版本: 1",
            ),
        );
    }
    assert.equal(p.plan.bodyJson + p.plan.reviewedJson, original);
    assert.deepEqual(p.calls, []);
});

test("unknown retries only its original intent; rejected edits; submitting and key conflict cannot discard it", () => {
    for (const [status, action] of [
        ["unknown", "retry"],
        ["rejected", "edit"],
        ["submitting", null],
        ["idempotency-conflict", null],
    ]) {
        const p = props();
        p.status = status;
        p.error = new ApiError("server", 409, "period_closed");
        const actions = buttons(BusinessCreditRevisionResult(p));
        assert.equal(actions.length, action ? 1 : 0);
        actions[0]?.props.onClick();
        assert.deepEqual(p.calls, action ? [action] : []);
        const html = markup(p);
        if (status === "unknown") {
            assert.match(html, /outcome is unknown/u);
            assert.match(html, /retry the original request/u);
            assert.doesNotMatch(
                html,
                /Edit rejected request|Close this revision receipt|Reopen with a reason/u,
            );
        }
        if (status === "rejected") assert.match(html, /every original and replacement date/u);
    }
});

test("verified correction and cancellation display original 18-place receipts, not today's state", () => {
    for (const action of ["correct", "cancel"]) {
        const p = confirmed(action),
            before = JSON.stringify(p.revisionReceipt);
        for (const locale of ["en", "zh"]) {
            p.locale = locale;
            const html = markup(p);
            assert(html.includes(id(8)));
            assert.match(html, /17\.230000000000000000 USD/u);
            assert(
                html.includes(
                    locale === "en"
                        ? "This receipt does not represent today"
                        : "此回执不代表今日的当前状态",
                ),
            );
            if (action === "correct") assert.match(html, /23\.000000000000000000 USD/u);
            else {
                assert(
                    html.includes(
                        locale === "en" ? "no zero-amount replacement" : "不写入零金额替代",
                    ),
                );
                assert.doesNotMatch(html, /Replacement fact reason|替代事实说明/u);
            }
            const actions = buttons(BusinessCreditRevisionResult(p));
            assert.equal(actions.length, 1);
            actions[0].props.onClick();
        }
        assert.deepEqual(p.calls, ["close", "close"]);
        assert.equal(JSON.stringify(p.revisionReceipt), before);
    }
});

test("auth-required hides private facts; malformed plans or unverified confirmed receipts expose no actions", () => {
    const hidden = props();
    hidden.status = "auth-required";
    assert.match(markup(hidden), /original user/u);
    assert.doesNotMatch(markup(hidden), /Fictional|17\.23|00000000/u);
    hidden.status = "idle";
    assert.equal(markup(hidden), "");
    for (const mutate of [
        (p) => (p.plan = { ...p.plan, bodyJson: "{" }),
        (p) => (p.plan = { ...p.plan, reviewedJson: "null" }),
        (p) => (p.plan = { ...p.plan, ledgerId: id(90) }),
        (p) => (p.revisionReceipt = null),
        (p) => (p.revisionReceipt.members[0].total_amount = "23.01"),
    ]) {
        const p = confirmed();
        mutate(p);
        assert.match(markup(p), /cannot be displayed safely/u);
        assert.doesNotMatch(markup(p), /Fictional|17\.23|00000000/u);
        assert.equal(buttons(BusinessCreditRevisionResult(p)).length, 0);
    }
});

test("long exact crypto quantities remain strings and original user text is escaped", () => {
    for (const amount of ["0.000000000000000001", "99999999999999999999.999999999999999999"]) {
        const f = cancelled();
        f.body.reason = "<script>fictional unsafe text</script>";
        f.review.current.members = [
            {
                ...f.review.current.members[0],
                net_amount: amount,
                tax_amount: "0",
                total_amount: amount,
            },
        ];
        f.review.asset = {
            ...f.review.asset,
            code: "ETH",
            asset_id: "ETH",
            kind: "native",
            network: "ethereum",
            scale: 18,
        };
        f.review.current.asset_id = "ETH";
        const p = props(f),
            original = p.plan.reviewedJson;
        const html = markup(p);
        assert(
            html.includes(
                amount.startsWith("0")
                    ? "0.000000000000000001 ETH"
                    : "99,999,999,999,999,999,999.999999999999999999 ETH",
            ),
        );
        assert.match(html, /&lt;script&gt;/u);
        assert.doesNotMatch(html, /<script>/u);
        assert.equal(p.plan.reviewedJson, original);
    }
});

test("dedicated revision errors are bilingual, preserve original intent and never echo private error details", () => {
    for (const name of [
        "period_closed",
        "version_conflict",
        "credit_cancelled",
        "credit_revision_version",
        "credit_date",
        "credit_exceeds_source",
        "credit_exceeds_open",
        "credit_revision_integrity",
        "credit_integrity",
        "entity_archived",
        "account_archived",
        "category_archived",
        "unknown_private_code",
    ]) {
        const error = new ApiError("server", 422, name, [
            { path: [], message: "Fictional private error input", type: "private" },
        ]);
        const zh = creditRevisionError(error, "zh"),
            en = creditRevisionError(error, "en");
        assert.notEqual(zh, en);
        assert.doesNotMatch(zh + en, /private/u);
    }
    for (const error of [
        new ApiError("network"),
        new ApiError("unauthorized", 401),
        new ApiError("forbidden", 403),
        new Error("Fictional private error input"),
    ]) {
        assert.doesNotMatch(creditRevisionError(error, "en"), /Refresh|refreshed list|private/u);
        assert.notEqual(creditRevisionError(error, "zh"), creditRevisionError(error, "en"));
    }
});
