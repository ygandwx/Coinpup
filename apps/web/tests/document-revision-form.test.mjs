import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { registerHooks } from "node:module";
import { test } from "node:test";
import ts from "typescript";
import { createElement, Fragment } from "react";
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
const { BusinessDocumentRevisionForm: Form } =
    await import("../src/BusinessDocumentRevisionForm.tsx");
const { documentRevisionFields } = await import("../src/document-revision-fields.ts");
const load = (name) => JSON.parse(readFileSync(new URL(name, import.meta.url), "utf8"));
const small = load("./document-revision-fixture.json"),
    large = load("./document-revision-200-fixture.json");
function props(locale = "en", action = "correct", big = false) {
    const f = structuredClone(big ? large : small),
        current = f.review.current,
        doc = current.document;
    const row = (identity) => ({
        id: identity,
        ledger_id: current.ledger_id,
        name: "Fictional selected reference",
        archived: false,
        version: 7,
    });
    const fields = documentRevisionFields(current, action);
    fields.reason = "Fictional reason / 原因";
    return {
        current,
        fields,
        locale,
        available: true,
        disabled: false,
        refs: {
            assets: [f.review.oldAsset, f.review.newAsset],
            parties: [{ ...row(doc.party_id), role: "supplier" }],
            categories: [{ ...row(doc.lines[0].category_id), kind: "expense" }],
            projects: [{ ...row(doc.lines[0].project_id) }],
        },
        onChange() {},
        onSubmit() {
            throw new Error("Rendering must never submit");
        },
        onBack() {},
    };
}
const html = (p) => renderToStaticMarkup(createElement(Form, p));
for (const locale of ["zh", "en"])
    for (const action of ["correct", "cancel"])
        test(`${locale} ${action} displays the full current facts and requires explicit review`, () => {
            const p = props(locale, action),
                before = JSON.stringify(p),
                out = html(p);
            assert.equal((out.match(/data-testid="document-current-line"/gu) ?? []).length, 3);
            assert.ok(out.includes(p.current.postings[1].journal_id));
            assert.ok(
                out.includes(locale === "zh" ? "零金额行，不生成分录" : "Zero amount; no posting"),
            );
            assert.ok(
                out.includes(
                    locale === "zh" ? "我已核对当前单据" : "I reviewed the current document",
                ),
            );
            assert.match(out, /<button type="submit" disabled=""/u);
            assert.equal(
                out.includes(
                    locale === "zh" ? "<h4>完整替代内容</h4>" : "<h4>Complete replacement</h4>",
                ),
                action === "correct",
            );
            assert.equal(JSON.stringify(p), before);
        });
test("the complete200 group renders every current zero line and replacement editor", () => {
    const p = props("en", "correct", true),
        out = html(p);
    assert.equal((out.match(/data-testid="document-current-line"/gu) ?? []).length, 200);
    assert.equal((out.match(/class="draft-line"/gu) ?? []).length, 200);
    assert.equal((out.match(/Zero amount; no posting/gu) ?? []).length, 67);
    assert.ok(out.includes(p.current.document.lines[199].id));
    assert.match(out, /<button type="button" disabled="">Add line<\/button>/u);
});
test("explicit refresh displays each actual selected version and shared identity scope", () => {
    const p = props();
    p.fields.refreshReferences = true;
    const out = html(p);
    assert.ok(out.includes("Reviewed reference versions"));
    assert.equal((out.match(/Version<!-- --> <!-- -->7|Version 7/gu) ?? []).length, 3);
    assert.ok(out.includes("shared by every replacement line with that identity"));
    assert.ok(out.includes("The issuer snapshot is retained"));
});
test("401 availability hides all private facts, reasons, identities and inputs", () => {
    const p = props();
    p.available = false;
    const out = html(p);
    assert.ok(out.includes("Sign in to reload"));
    for (const privateValue of [
        p.current.document_id,
        p.current.document.lines[0].description,
        p.fields.reason,
        p.current.postings[0].journal_id,
    ])
        assert.equal(out.includes(privateValue), false);
    assert.equal(out.includes("<form"), false);
});
test("inconsistent current facts and terminal documents cannot render an editable revision", () => {
    for (const mutate of [
        (p) => {
            p.current.postings.pop();
        },
        (p) => {
            p.current.status = "cancelled";
            p.current.document = null;
            p.current.postings = [];
        },
    ]) {
        const p = props();
        mutate(p);
        const out = html(p);
        assert.ok(out.includes("cannot be revised"));
        assert.equal(out.includes("<form"), false);
        assert.equal(out.includes(p.current.document_id), false);
    }
});
test("cancel remains available with disabled old assets and no master catalog", () => {
    const p = props("en", "cancel");
    p.refs.assets[0].enabled = false;
    p.refs.parties = [];
    p.refs.categories = [];
    p.refs.projects = [];
    const out = html(p);
    assert.ok(out.includes("Cancel whole document"));
    assert.equal(out.includes("Complete replacement"), false);
    assert.match(out, /type="checkbox"\/>/u);
});
test("malformed amounts remain editable without floating conversion or an enabled submit", () => {
    const p = props();
    p.fields.replacement.lines[0].unit_price = "0.000000000000000001";
    const out = html(p);
    assert.ok(out.includes('value="0.000000000000000001"'));
    assert.ok(out.includes("Check the asset and each line"));
    assert.match(out, /<button type="submit" disabled=""/u);
});
test("frozen state disables editable controls and keeps the full reviewed text", () => {
    const p = props();
    p.disabled = true;
    const out = html(p);
    assert.match(out, /<fieldset disabled="" class="draft-fields"/u);
    assert.ok(out.includes(p.fields.reason));
    assert.match(out, /<button type="button" disabled="">Back<\/button>/u);
});
test("separate form instances have distinct accessible control identities", () => {
    const out = renderToStaticMarkup(
        createElement(Fragment, null, createElement(Form, props()), createElement(Form, props())),
    );
    const ids = [...out.matchAll(/\sid="([^"]+)"/gu)].map((m) => m[1]);
    assert.ok(ids.length > 30);
    assert.equal(new Set(ids).size, ids.length);
    const labels = [...out.matchAll(/\sfor="([^"]+)"/gu)].map((m) => m[1]);
    assert.ok(labels.every((label) => ids.includes(label)));
});
test("saved and typed text remains escaped data in both languages", () => {
    const p = props("zh");
    p.fields.reason = "<script>fictional</script>";
    p.current.document.notes = "<img src=x onerror=fictional>";
    const out = html(p);
    assert.equal(out.includes("<script>"), false);
    assert.equal(out.includes("<img src=x"), false);
    assert.ok(out.includes("&lt;script&gt;"));
    assert.ok(out.includes("&lt;img src=x"));
});
