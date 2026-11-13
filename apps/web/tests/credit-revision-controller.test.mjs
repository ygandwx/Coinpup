import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { test } from "node:test";
import ts from "typescript";
import { renderToStaticMarkup } from "react-dom/server";
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
const { PendingBusinessCreditController } = await import("../src/pending-business-credit.ts");
const { isCreditRevisionPlan } = await import("../src/credit-revision-intent.ts");
const { BusinessCreditResult } = await import("../src/BusinessCreditResult.tsx");
const { ApiError } = await import("../src/api.ts");
import { fixture, ledger, creditId, documentId, id } from "./credit-revision-fixture.mjs";
const session = { user: { id: "fictional-owner" }, csrf_token: "fictional-csrf-initial" };
function revision(action = "correct", target = creditId) {
    const f = fixture();
    f.action = action;
    f.review.current.credit_id = target;
    f.review.current.members = [f.review.current.members[0]];
    f.body.replacement.lines = [f.body.replacement.lines[1]];
    const result = {
        kind: "business_credit_revision",
        credit_id: target,
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
    if (action === "cancel") {
        delete f.body.replacement;
        delete f.review.capacity;
        f.review.asset.enabled = false;
    }
    return { ...f, target, result };
}
const start = (controller, f, auth = session) =>
    controller.startRevision(auth, ledger, f.target, f.action, f.body, f.review);
const code = (expected) => (error) => error instanceof ApiError && error.code === expected;

for (const action of ["correct", "cancel"])
    test(`${action} retries only frozen POST through response loss, live rejections and same-user 401 recovery`, async (t) => {
        const f = revision(action),
            calls = [],
            reentrant = [];
        const statuses = [null, 404, 409, 422, 503, 401, 200];
        t.mock.method(globalThis, "fetch", async (path, init) => {
            calls.push({ path, init });
            const status = statuses.shift();
            if (status === null) throw new TypeError("Fictional lost response");
            return Response.json(
                status === 200
                    ? f.result
                    : {
                          detail: {
                              code: status === 409 ? "version_conflict" : "fictional_rejection",
                          },
                      },
                { status },
            );
        });
        const controller = new PendingBusinessCreditController();
        controller.setOwner(session.user.id);
        const unsubscribe = controller.subscribe(() => {
            if (controller.getSnapshot().status === "submitting" && !reentrant.length) {
                reentrant.push(start(controller, f).catch((error) => error));
                reentrant.push(controller.start(session, ledger, {}).catch((error) => error));
            }
        });
        const first = start(controller, f);
        await assert.rejects(start(controller, f), code("pending_credit_unresolved"));
        await first;
        assert.equal(controller.getSnapshot().status, "unknown");
        const frozen = controller.getSnapshot().plan;
        assert(Object.isFrozen(frozen));
        assert(isCreditRevisionPlan(frozen));
        assert.equal(frozen.ownerId, session.user.id);
        assert.equal(frozen.ledgerId, ledger);
        assert.equal(frozen.action, action);
        assert.equal(frozen.creditId, creditId);
        assert(!JSON.stringify(frozen).includes(session.csrf_token));
        f.body.reason = "Fictional later edited form";
        f.review.current.version = 999;
        assert.equal(JSON.parse(frozen.bodyJson).reason, "  Fictional correction 中文  ");
        assert.equal(JSON.parse(frozen.reviewedJson).current.version, 3);
        for (let i = 0; i < 4; i++) {
            await controller.retry(session);
            assert.equal(controller.getSnapshot().status, "unknown");
            assert.equal(controller.dismiss(), false);
            assert.equal(controller.getSnapshot().plan, frozen);
        }
        await controller.retry(session);
        assert.equal(controller.getSnapshot().status, "auth-required");
        controller.setOwner(null);
        assert.equal(controller.getSnapshot().plan, null);
        assert.equal(controller.getSnapshot().receipt, null);
        assert.equal(controller.getSnapshot().revisionReceipt, null);
        controller.setOwner(session.user.id);
        assert.equal(controller.getSnapshot().status, "unknown");
        assert.equal(calls.length, 6); // Signing in never sends a new request automatically.
        await controller.retry({ ...session, csrf_token: "fictional-csrf-fresh" });
        const saved = controller.getSnapshot();
        assert.equal(saved.status, "confirmed");
        assert.equal(saved.receipt, null);
        assert.deepEqual(saved.revisionReceipt, f.result);
        assert(Object.isFrozen(saved.revisionReceipt.members[0]));
        assert(
            calls.every(
                ({ path, init }) =>
                    path ===
                        `/api/v1/ledgers/${ledger}/business-credits/${creditId}/${action === "correct" ? "corrections" : "cancellations"}` &&
                    init.method === "POST" &&
                    init.body === frozen.bodyJson &&
                    init.headers["Idempotency-Key"] === frozen.key,
            ),
        );
        assert.equal(calls.at(-1).init.headers["X-CSRF-Token"], "fictional-csrf-fresh");
        assert((await Promise.all(reentrant)).every(code("pending_credit_unresolved")));
        await assert.rejects(controller.retry(session), code("credit_retry_unavailable"));
        unsubscribe();
        assert(controller.dismiss());
        assert.equal(controller.getSnapshot().revisionReceipt, null);
    });

test("known first rejection permits a new key; idempotency conflict never clears the original plan", async () => {
    const f = revision(),
        calls = [];
    const controller = new PendingBusinessCreditController(async (_auth, frozen) => {
        calls.push(frozen);
        if (calls.length === 1) throw new ApiError("server", 409, "period_closed");
        return { ...f.result, reason: "Fictional corrected reason" };
    });
    controller.setOwner(session.user.id);
    await start(controller, f);
    assert.equal(controller.getSnapshot().status, "rejected");
    f.body.reason = " Fictional corrected reason ";
    await start(controller, f);
    assert.equal(controller.getSnapshot().status, "confirmed");
    assert.notEqual(calls[0].key, calls[1].key);
    assert.notEqual(calls[0].bodyJson, calls[1].bodyJson);
    const conflict = new PendingBusinessCreditController(async () => {
        throw new ApiError("server", 409, "idempotency_conflict");
    });
    conflict.setOwner(session.user.id);
    await start(conflict, f);
    assert.equal(conflict.getSnapshot().status, "idempotency-conflict");
    assert.equal(conflict.dismiss(), false);
    await assert.rejects(start(conflict, f), code("pending_credit_unresolved"));
    await assert.rejects(conflict.retry(session), code("credit_retry_unavailable"));
});

test("malformed or unrelated success remains unknown until the original complete revision receipt arrives", async () => {
    for (const response of [
        (f) => f.review.current,
        (f) => ({ ...f.result, kind: "business_credit" }),
        (f) => ({ ...f.result, members: [] }),
        (f) => ({ ...f.result, document_id: id(99) }),
    ]) {
        const f = revision();
        let calls = 0;
        const controller = new PendingBusinessCreditController(async () =>
            ++calls === 1 ? response(f) : f.result,
        );
        controller.setOwner(session.user.id);
        await start(controller, f);
        assert.equal(controller.getSnapshot().status, "unknown");
        assert.equal(controller.getSnapshot().revisionReceipt, null);
        assert.equal(controller.dismiss(), false);
        await controller.retry(session);
        assert.equal(controller.getSnapshot().status, "confirmed");
        assert.deepEqual(controller.getSnapshot().revisionReceipt, f.result);
    }
});

test("different owners and explicit logout isolate a late revision response from a newer command", async () => {
    for (const logout of [false, true]) {
        const original = revision(),
            next = revision("cancel", id(22));
        const nextSession = logout ? session : { ...session, user: { id: "fictional-other" } };
        let finish;
        const controller = new PendingBusinessCreditController(async (_auth, frozen) => {
            if (frozen.creditId === id(22)) return next.result;
            return new Promise((resolve) => {
                finish = () => resolve(original.result);
            });
        });
        controller.setOwner(session.user.id);
        const pending = start(controller, original);
        await Promise.resolve();
        controller.setOwner(logout ? null : nextSession.user.id, logout);
        controller.setOwner(nextSession.user.id);
        assert.equal(controller.getSnapshot().plan, null);
        await start(controller, next, nextSession);
        const saved = controller.getSnapshot();
        assert.equal(saved.status, "confirmed");
        assert.equal(saved.revisionReceipt.credit_id, id(22));
        finish();
        await pending;
        assert.equal(controller.getSnapshot(), saved);
        if (!logout)
            await assert.rejects(start(controller, original), code("credit_owner_changed"));
    }
});

test("unauthorized or invalid revision intent never creates a plan or sends a request", async () => {
    let calls = 0;
    const f = revision(),
        controller = new PendingBusinessCreditController(async () => {
            calls++;
        });
    await assert.rejects(start(controller, f), code("credit_owner_changed"));
    controller.setOwner(session.user.id);
    f.body.expected_version = 99;
    await assert.rejects(start(controller, f), code("invalid_credit_intent"));
    assert.equal(calls, 0);
    assert.equal(controller.getSnapshot().status, "idle");
    assert.equal(controller.getSnapshot().plan, null);
});

test("legacy Result rejects revision plans before JSON parsing and still renders creation details", () => {
    const callbacks = { locale: "en", onRetry() {}, onEdit() {}, onClose() {} };
    const frame = {
        ownerId: session.user.id,
        ledgerId: ledger,
        key: "fictional-key",
        bodyJson: "{",
        reviewedJson: "{",
    };
    for (const action of ["correct", "cancel"]) {
        const pending = {
            status: "unknown",
            plan: { ...frame, creditId, action },
            receipt: null,
            revisionReceipt: null,
            error: null,
        };
        assert.equal(BusinessCreditResult({ ...callbacks, pending }), null);
    }
    const f = fixture(),
        body = {
            id: creditId,
            document_id: documentId,
            expected_version: 2,
            ...f.body.replacement,
        };
    const pending = {
        status: "unknown",
        plan: { ...frame, bodyJson: JSON.stringify(body), reviewedJson: JSON.stringify(f.review) },
        receipt: null,
        revisionReceipt: null,
        error: null,
    };
    assert(!isCreditRevisionPlan(pending.plan));
    const markup = renderToStaticMarkup(BusinessCreditResult({ ...callbacks, pending }));
    assert.match(markup, /Original credit submission/u);
    assert.match(markup, /Retry original request/u);
});
