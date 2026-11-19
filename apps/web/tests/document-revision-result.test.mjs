import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { registerHooks } from "node:module";
import { test } from "node:test";
import ts from "typescript";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
registerHooks({
    resolve(s, c, next) {
        if (s.startsWith(".") && !/\.[a-z]+$/u.test(s) && c.parentURL?.includes("/src/"))
            s += existsSync(new URL(`${s}.tsx`, c.parentURL)) ? ".tsx" : ".ts";
        return next(s, c);
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
const { BusinessDocumentRevisionResult: Result, documentRevisionError } =
    await import("../src/BusinessDocumentRevisionResult.tsx");
const { documentRevisionDisplay } = await import("../src/document-revision-display.ts");
const { ApiError } = await import("../src/api.ts");
const load = (name) => JSON.parse(readFileSync(new URL(name, import.meta.url), "utf8"));
const small = load("./document-revision-fixture.json"),
    large = load("./document-revision-200-fixture.json");
function props(status = "unknown", locale = "en", big = false, cancel = false) {
    const f = structuredClone(big ? large : small);
    if (cancel) {
        f.input.action = f.receipt.action = "cancel";
        delete f.input.body.replacement;
        f.receipt.replacement = null;
    }
    const plan = {
        ownerId: "fictional-owner",
        ledgerId: f.review.current.ledger_id,
        documentId: f.review.current.document_id,
        action: f.input.action,
        key: "fictional-original-key",
        bodyJson: JSON.stringify(f.input.body),
        reviewedJson: JSON.stringify(f.review),
    };
    return {
        pending: {
            status,
            plan,
            receipt: null,
            revisionReceipt: status === "confirmed" ? f.receipt : null,
            error: null,
        },
        locale,
        onRetry() {
            throw new Error("Render never retries");
        },
        onEdit() {},
        onClose() {},
    };
}
const html = (p) => renderToStaticMarkup(createElement(Result, p));
for (const locale of ["zh", "en"])
    test(`${locale} unknown result shows frozen input/preview and only original retry`, () => {
        const p = props("unknown", locale),
            before = JSON.stringify(p),
            out = html(p);
        assert.ok(out.includes(locale === "zh" ? "提交结果未知" : "submission outcome is unknown"));
        assert.ok(
            out.includes(
                locale === "zh"
                    ? "已冻结替代输入与金额预览"
                    : "Frozen replacement input and amount preview",
            ),
        );
        assert.ok(out.includes(locale === "zh" ? "重试原请求" : "Retry original request"));
        assert.equal((out.match(/<button/gu) ?? []).length, 1);
        assert.equal(
            out.includes(
                locale === "zh"
                    ? "本次修订的完整回执已核对"
                    : "complete receipt for this revision is verified",
            ),
            false,
        );
        assert.equal(JSON.stringify(p), before);
    });
for (const cancel of [false, true])
    test(`complete200 fixed ${cancel ? "cancel" : "correct"} result displays all facts/reversals`, () => {
        const p = props("confirmed", "en", true, cancel),
            out = html(p);
        assert.ok(out.includes("complete receipt for this revision is verified"));
        assert.equal(
            (out.match(/data-testid="document-version-line"/gu) ?? []).length,
            cancel ? 200 : 400,
        );
        for (const reversal of p.pending.revisionReceipt.reversals)
            assert.ok(out.includes(reversal.journal_id));
        assert.ok(out.includes("Close"));
        assert.equal(out.includes("Retry original request"), false);
        assert.equal(out.includes("Fixed replacement facts"), !cancel);
    });
test("confirmed state with malformed final posting or changed frozen source fails closed", () => {
    for (const mutate of [
        (p) => {
            p.pending.revisionReceipt.replacement.lines[199].posting.operation_version = 2;
        },
        (p) => {
            const body = JSON.parse(p.pending.plan.bodyJson);
            body.replacement.lines[199].unit_price = "3.00";
            p.pending.plan.bodyJson = JSON.stringify(body);
        },
        (p) => {
            p.pending.revisionReceipt = JSON.parse(p.pending.plan.reviewedJson).current;
        },
    ]) {
        const p = props("confirmed", "en", true);
        mutate(p);
        const out = html(p);
        assert.ok(out.includes("cannot be displayed safely"));
        assert.equal(out.includes("<button"), false);
        assert.equal(out.includes(p.pending.plan.documentId), false);
    }
});
test("auth-required hides private plan and receipt even when caller accidentally supplies both", () => {
    const p = props("confirmed");
    p.pending.status = "auth-required";
    const out = html(p);
    assert.ok(out.includes("original user"));
    assert.equal(out.includes(p.pending.plan.documentId), false);
    assert.equal(out.includes(p.pending.revisionReceipt.revision_id), false);
    assert.equal(out.includes("<button"), false);
});
test("unknown ignores a supplied success-shaped receipt and never invents new posting identities", () => {
    const p = props("confirmed"),
        revision = p.pending.revisionReceipt.revision_id,
        futureJournal = p.pending.revisionReceipt.replacement.lines[2].posting.journal_id;
    p.pending.status = "unknown";
    const out = html(p);
    assert.equal(out.includes(revision), false);
    assert.equal(out.includes(futureJournal), false);
    assert.ok(out.includes("Preview amounts do not prove posting"));
    const display = documentRevisionDisplay(p.pending.plan, "unknown", p.pending.revisionReceipt);
    assert.equal(display.saved, null);
    assert.ok(display.replacement.lines.every((r) => r.posting === undefined));
});
test("only an explicit rejection permits reload/new review and close", () => {
    const p = props("rejected"),
        out = html(p);
    assert.ok(out.includes("explicitly rejected"));
    assert.ok(out.includes("Reload and review"));
    assert.ok(out.includes("Close"));
    assert.equal(out.includes("Retry original request"), false);
    for (const status of ["submitting", "idempotency-conflict"]) {
        const blocked = html(props(status));
        assert.equal(blocked.includes("<button"), false);
    }
});
test("idle or an unrelated confirmation intent does not render revision details", () => {
    assert.equal(html(props("idle")), "");
    const p = props();
    delete p.pending.plan.action;
    assert.equal(html(p), "");
});
for (const locale of ["zh", "en"])
    test(`${locale} errors give bounded revision advice without reflecting server messages`, () => {
        for (const code of [
            "period_closed",
            "document_revision_dependency",
            "version_conflict",
            "document_reference_review_required",
            "document_cancelled",
            "document_revision_version",
            "document_revision_integrity",
            "invalid_response",
            "unexpected-fictional-private-diagnostic",
        ]) {
            const e = new ApiError("server", 409, code),
                text = documentRevisionError(e, locale);
            assert.ok(text.length > 10);
            assert.equal(text.includes(code), false);
        }
        const unknown = props("unknown", locale);
        unknown.pending.error = new ApiError("server", 409, "version_conflict");
        const out = html(unknown);
        assert.equal((out.match(/<button/gu) ?? []).length, 1);
    });
