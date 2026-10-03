import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

// Vite resolves extensionless source imports; native Node tests need the same local rule.
registerHooks({
    resolve(specifier, context, nextResolve) {
        if (
            specifier.startsWith(".") &&
            !/\.[a-z]+$/u.test(specifier) &&
            context.parentURL?.includes("/src/")
        ) {
            return nextResolve(`${specifier}.ts`, context);
        }
        return nextResolve(specifier, context);
    },
});
const { ApiError } = await import("../src/api.ts");
const { PendingCommandController } = await import("../src/pending-command.ts");
const { createAsset, updateAsset, listOperations, getOperation, getOperationHistory } =
    await import("../src/ledger-api.ts");
const { postingInputFromState, replacementFromInput } = await import("../src/revision-input.ts");

const session = {
    user: { id: "11111111-1111-4111-8111-111111111111", username: "fictional-owner" },
    csrf_token: "fictional-csrf-old",
};
const other = {
    user: { id: "22222222-2222-4222-8222-222222222222", username: "fictional-owner" },
    csrf_token: "fictional-csrf-other",
};
const ledger = "33333333-3333-4333-8333-333333333333";
function input(kind = "expense") {
    const meta = { transaction_date: "2026-01-02", description: "Fictional test" };
    if (kind === "transfer")
        return {
            kind,
            body: {
                ...meta,
                source_account_id: "account-source",
                destination_account_id: "account-destination",
                asset_id: "USD",
                amount: "1.00",
                fees: [],
            },
        };
    if (kind === "exchange")
        return {
            kind,
            body: {
                ...meta,
                source_account_id: "account-source",
                destination_account_id: "account-source",
                source_asset_id: "USD",
                source_amount: "1.00",
                destination_asset_id: "EUR",
                destination_amount: "0.90",
                fees: [],
            },
        };
    const body = { ...meta, account_id: "account-source", asset_id: "USD", amount: "1.00" };
    if (kind !== "opening")
        Object.assign(body, {
            recognition_date: "2026-01-01",
            splits: [{ category_id: "category", amount: "1.00" }],
            fees: [],
        });
    return { kind, body };
}
function receipt(command) {
    const body = JSON.parse(command.bodyJson);
    const result = {
        ...body,
        id: command.operationId,
        ledger_id: command.ledgerId,
        journal_id: "journal",
        kind: command.kind,
        version: 1,
        recognition_date: body.recognition_date ?? body.transaction_date,
        created_at: "2026-01-03T00:00:00Z",
    };
    if (["opening", "income", "expense"].includes(command.kind)) result.splits ??= [];
    return result;
}
function current(command) {
    return {
        id: command.operationId,
        ledger_id: command.ledgerId,
        kind: command.kind,
        version: 1,
        status: "active",
        latest_posting: receipt(command),
        updated_at: "2026-01-03T00:00:00Z",
    };
}
function controllerWith(io = {}) {
    const controller = new PendingCommandController({
        post: async (_session, command) => receipt(command),
        read: async (_session, command) => current(command),
        ...io,
    });
    controller.setOwner(session.user.id);
    return controller;
}
function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((yes, no) => {
        resolve = yes;
        reject = no;
    });
    return { promise, resolve, reject };
}

test("a committed request with a dropped response reuses exact body, ledger and key", async (t) => {
    const calls = [];
    const committed = new Map();
    t.mock.method(globalThis, "fetch", async (path, init) => {
        calls.push({ path, init });
        const key = init.headers["Idempotency-Key"];
        const body = JSON.parse(init.body);
        if (!committed.has(key)) {
            const command = {
                operationId: body.id,
                ledgerId: ledger,
                bodyJson: init.body,
                kind: "expense",
            };
            committed.set(key, receipt(command));
            throw new TypeError("Response dropped after commit");
        }
        return Response.json(committed.get(key), { status: 201 });
    });
    const controller = new PendingCommandController();
    controller.setOwner(session.user.id);
    const editable = input();
    await controller.submit(session, ledger, editable);
    const first = controller.getSnapshot();
    assert.equal(first.status, "unknown");
    assert.equal(controller.dismiss(), false);
    assert.equal(controller.clear(), false);
    editable.body.amount = "2.00";
    editable.body.splits[0].amount = "2.00";
    await assert.rejects(controller.submit(session, "another-ledger", editable), {
        code: "pending_command_unresolved",
    });
    await controller.retry({ ...session, csrf_token: "fictional-csrf-new" });
    assert.equal(controller.getSnapshot().status, "confirmed");
    assert.equal(committed.size, 1);
    assert.equal(calls.length, 2);
    assert.equal(calls[0].path, calls[1].path);
    assert.equal(calls[0].init.body, calls[1].init.body);
    assert.equal(
        calls[0].init.headers["Idempotency-Key"],
        calls[1].init.headers["Idempotency-Key"],
    );
    assert.equal(calls[1].init.headers["X-CSRF-Token"], "fictional-csrf-new");
    assert.notEqual(first.command.operationId, first.command.key);
    assert.ok(!JSON.stringify(first.command).includes("csrf"));
    assert.equal(controller.dismiss(), true);
});

test("double clicks share one request and one immutable snapshot", async () => {
    const pending = deferred();
    let calls = 0;
    let command;
    const controller = controllerWith({
        post: async (_session, captured) => {
            calls += 1;
            command = captured;
            return pending.promise;
        },
    });
    const one = controller.submit(session, ledger, input());
    const snapshot = controller.getSnapshot();
    const two = controller.submit(session, "another-ledger", input("income"));
    assert.equal(one, two);
    assert.equal(controller.getSnapshot(), snapshot);
    await Promise.resolve();
    assert.equal(calls, 1);
    pending.resolve(receipt(command));
    await one;
    assert.equal(controller.getSnapshot().status, "confirmed");
    assert.equal(controller.getSnapshot().command.ledgerId, ledger);
});

test("401 hides content and same owner must explicitly resume with the new CSRF token", async () => {
    const attempts = [];
    const controller = controllerWith({
        post: async (activeSession, command) => {
            attempts.push({ session: activeSession, command });
            if (attempts.length === 1) throw new ApiError("unauthorized", 401);
            return receipt(command);
        },
    });
    await controller.submit(session, ledger, input());
    const original = controller.getSnapshot().command;
    assert.equal(controller.getSnapshot().status, "auth-required");
    controller.setOwner(null);
    assert.deepEqual(controller.getSnapshot(), {
        status: "auth-required",
        command: null,
        receipt: null,
        revisionReceipt: null,
        current: null,
        error: null,
        checking: false,
    });
    await assert.rejects(controller.retry(session), { code: "pending_owner_mismatch" });
    controller.setOwner(session.user.id);
    assert.equal(controller.getSnapshot().command, original);
    assert.equal(controller.getSnapshot().status, "unknown");
    assert.equal(controller.getSnapshot().error, null);
    assert.equal(attempts.length, 1);
    await controller.retry({ ...session, csrf_token: "fictional-csrf-new" });
    assert.equal(attempts[1].session.csrf_token, "fictional-csrf-new");
    assert.equal(attempts[1].command, original);
});

test("another owner with the same username cannot see or retry the old command", async () => {
    const controller = controllerWith({
        post: async () => {
            throw new ApiError("network");
        },
    });
    await controller.submit(session, ledger, input());
    controller.setOwner(null);
    controller.setOwner(other.user.id);
    assert.equal(controller.getSnapshot().status, "idle");
    assert.equal(controller.getSnapshot().command, null);
    await assert.rejects(controller.retry(session), { code: "pending_owner_mismatch" });
    controller.setOwner(session.user.id);
    assert.equal(controller.getSnapshot().command, null);
});

test("explicit logout cancels a queued dispatch and late responses cannot replace a new owner", async () => {
    let calls = 0;
    const first = controllerWith({
        post: async (_session, command) => {
            calls += 1;
            return receipt(command);
        },
    });
    const queued = first.submit(session, ledger, input());
    first.setOwner(null, true);
    await queued;
    assert.equal(calls, 0);
    assert.equal(first.getSnapshot().status, "idle");

    const pending = deferred();
    let captured;
    const controller = controllerWith({
        post: async (activeSession, command) => {
            if (activeSession.user.id === session.user.id) {
                captured = command;
                return pending.promise;
            }
            return receipt(command);
        },
    });
    const oldRequest = controller.submit(session, ledger, input());
    await Promise.resolve();
    controller.setOwner(null, true);
    controller.setOwner(other.user.id);
    await controller.submit(other, "other-ledger", input("opening"));
    const newer = controller.getSnapshot();
    pending.resolve(receipt(captured));
    await oldRequest;
    assert.equal(controller.getSnapshot(), newer);
    assert.equal(newer.command.ownerId, other.user.id);
});

test("404 is not proof of failure, then authoritative current state can confirm a cancelled operation", async () => {
    let reads = 0;
    const controller = controllerWith({
        post: async () => {
            throw new ApiError("network");
        },
        read: async (_session, command) => {
            reads += 1;
            if (reads === 1) throw new ApiError("server", 404, "not_found");
            return {
                ...current(command),
                status: "cancelled",
                version: 2,
                cancellation: {
                    version: 2,
                    reversal_journal_id: "reversal",
                    reason: "Fictional cancellation",
                    recorded_at: "2026-01-03T00:00:00Z",
                },
            };
        },
    });
    await controller.submit(session, ledger, input());
    await controller.reconcile(session);
    assert.equal(controller.getSnapshot().status, "unknown");
    assert.equal(controller.dismiss(), false);
    await controller.reconcile(session);
    assert.equal(controller.getSnapshot().status, "confirmed");
    assert.equal(controller.getSnapshot().current.status, "cancelled");
    assert.equal(controller.getSnapshot().receipt, null);
    assert.equal(controller.dismiss(), true);
});

test("definite validation failure permits a new intent, but rejection after unknown remains locked", async () => {
    let calls = 0;
    const controller = controllerWith({
        post: async () => {
            calls += 1;
            throw calls === 2
                ? new ApiError("network")
                : new ApiError("server", 422, "amount_precision");
        },
    });
    await controller.submit(session, ledger, input());
    assert.equal(controller.getSnapshot().status, "rejected");
    const first = controller.getSnapshot().command;
    assert.equal(controller.dismiss(), true);
    await controller.submit(session, ledger, input());
    assert.notEqual(controller.getSnapshot().command.key, first.key);
    await controller.retry(session);
    assert.equal(controller.getSnapshot().status, "unknown");
    assert.equal(controller.dismiss(), false);
});

test("idempotency conflict is not retried or dismissed automatically", async () => {
    const controller = controllerWith({
        post: async () => {
            throw new ApiError("server", 409, "idempotency_conflict");
        },
        read: async () => {
            throw new ApiError("server", 404, "not_found");
        },
    });
    await controller.submit(session, ledger, input());
    assert.equal(controller.getSnapshot().status, "idempotency-conflict");
    assert.equal(controller.dismiss(), false);
    await assert.rejects(controller.retry(session), { code: "pending_retry_unavailable" });
    await controller.reconcile(session);
    assert.equal(controller.getSnapshot().status, "idempotency-conflict");
});

test("broken JSON and malformed success identities remain unknown", async (t) => {
    const mock = t.mock.method(
        globalThis,
        "fetch",
        async () => new Response("broken", { status: 201 }),
    );
    const controller = new PendingCommandController();
    controller.setOwner(session.user.id);
    await controller.submit(session, ledger, input());
    assert.equal(controller.getSnapshot().status, "unknown");
    mock.mock.mockImplementation(async () =>
        Response.json({ id: "wrong-operation" }, { status: 201 }),
    );
    await controller.retry(session);
    assert.equal(controller.getSnapshot().status, "unknown");
    assert.equal(controller.getSnapshot().error.code, "invalid_response");
    assert.equal(controller.dismiss(), false);
});

test("snapshots are stable until change, unsubscribe works, and same owner does not erase data", async () => {
    const controller = controllerWith();
    const events = [];
    const unsubscribe = controller.subscribe(() => events.push(controller.getSnapshot()));
    assert.equal(controller.getSnapshot(), controller.getSnapshot());
    controller.setOwner(session.user.id);
    assert.equal(events.length, 0);
    await controller.submit(session, ledger, input());
    const saved = controller.getSnapshot();
    controller.setOwner(session.user.id);
    assert.equal(controller.getSnapshot(), saved);
    unsubscribe();
    const count = events.length;
    controller.dismiss();
    assert.equal(events.length, count);
});

test("all five financial endpoints send a mandatory stable key; asset mutations use their own contract", async (t) => {
    const paths = {
        opening: "opening-balances",
        income: "income",
        expense: "expenses",
        transfer: "transfers",
        exchange: "exchanges",
    };
    const calls = [];
    t.mock.method(globalThis, "fetch", async (path, init) => {
        calls.push({ path, init });
        const body = JSON.parse(init.body ?? "{}");
        const kind = Object.keys(paths).find((candidate) => path.endsWith(`/${paths[candidate]}`));
        return Response.json(
            kind
                ? receipt({ bodyJson: init.body, operationId: body.id, ledgerId: ledger, kind })
                : [],
        );
    });
    for (const kind of Object.keys(paths)) {
        const controller = new PendingCommandController();
        controller.setOwner(session.user.id);
        await controller.submit(session, ledger, input(kind));
        assert.equal(controller.getSnapshot().status, "confirmed");
        assert.ok(calls.at(-1).path.endsWith(paths[kind]));
        assert.match(calls.at(-1).init.headers["Idempotency-Key"], /^[!-~]{1,128}$/u);
    }
    await createAsset(session.csrf_token, {
        code: "USDC",
        kind: "token",
        scale: 6,
        network: "fictional",
        token_reference: "demo",
    });
    assert.equal(calls.at(-1).path, "/api/v1/assets");
    assert.equal(calls.at(-1).init.headers["Idempotency-Key"], undefined);
    await updateAsset(session.csrf_token, "token:fictional:demo", {
        expected_version: 1,
        enabled: false,
    });
    assert.equal(calls.at(-1).path, "/api/v1/assets/token%3Afictional%3Ademo");
    assert.equal(calls.at(-1).init.method, "PATCH");
    await listOperations(ledger, { status: "cancelled", limit: 25, offset: 50 });
    assert.equal(
        calls.at(-1).path,
        `/api/v1/ledgers/${ledger}/operations?limit=25&offset=50&status=cancelled`,
    );
    await getOperation(ledger, "operation");
    assert.equal(calls.at(-1).path, `/api/v1/ledgers/${ledger}/operations/operation`);
    const count = calls.length;
    await assert.rejects(listOperations(ledger, { limit: 201 }), { code: "invalid_pagination" });
    assert.equal(calls.length, count);
});

const operationId = "44444444-4444-4444-8444-444444444444";
function correction(kind = "expense", version = 1) {
    return {
        expected_version: version,
        reason: "Fictional correction",
        replacement: replacementFromInput(input(kind)),
    };
}
function stateAt(command, version, status = "active") {
    const request = JSON.parse(command.bodyJson);
    const fields = command.action === "correct" ? request.replacement : input(command.kind).body;
    const posting = receipt({ ...command, bodyJson: JSON.stringify(fields) });
    posting.version = status === "cancelled" ? version - 1 : version;
    const state = {
        id: command.operationId,
        ledger_id: command.ledgerId,
        kind: command.kind,
        version,
        status,
        latest_posting: posting,
        updated_at: "2026-01-04T00:00:00Z",
    };
    if (status === "cancelled")
        state.cancellation = {
            version,
            reversal_journal_id: "reversal",
            reason: "Fictional cancellation",
            recorded_at: "2026-01-04T00:00:00Z",
        };
    return state;
}

test("revision GET observations cannot confirm this key, even when current is newer or cancelled", async () => {
    let version = 1;
    const controller = controllerWith({
        post: async () => {
            throw new ApiError("network");
        },
        read: async (_session, command) =>
            stateAt(command, version, version === 3 ? "cancelled" : "active"),
    });
    await controller.correct(session, ledger, operationId, correction());
    const frozen = controller.getSnapshot().command;
    for (version of [1, 2, 3]) {
        await controller.reconcile(session);
        assert.equal(controller.getSnapshot().status, "unknown");
        assert.equal(controller.getSnapshot().current.version, version);
        assert.equal(controller.getSnapshot().revisionReceipt, null);
        assert.equal(controller.getSnapshot().command, frozen);
        assert.equal(controller.dismiss(), false);
    }
});

test("dropped correction response replays its old receipt while preserving a newer observed cancellation", async (t) => {
    const calls = [];
    let original;
    let stored;
    t.mock.method(globalThis, "fetch", async (path, init) => {
        if (init.method === "POST") {
            calls.push({ path, init });
            if (!stored) {
                const body = JSON.parse(init.body);
                original = {
                    action: "correct",
                    kind: "expense",
                    operationId,
                    ledgerId: ledger,
                    expectedVersion: body.expected_version,
                    bodyJson: init.body,
                };
                stored = stateAt(original, 2);
                throw new TypeError("Commit succeeded but response was dropped");
            }
            return Response.json(stored);
        }
        return Response.json(stateAt(original, 3, "cancelled"));
    });
    const controller = new PendingCommandController();
    controller.setOwner(session.user.id);
    const body = correction();
    await controller.correct(session, ledger, operationId, body);
    body.expected_version = 3;
    body.reason = "Edited after original submission";
    body.replacement.amount = "999.00";
    await controller.reconcile(session);
    assert.equal(controller.getSnapshot().status, "unknown");
    await controller.retry(session);
    const snapshot = controller.getSnapshot();
    assert.equal(snapshot.status, "confirmed");
    assert.equal(snapshot.receipt, null);
    assert.equal(snapshot.revisionReceipt.version, 2);
    assert.equal(snapshot.revisionReceipt.status, "active");
    assert.equal(snapshot.current.version, 3);
    assert.equal(snapshot.current.status, "cancelled");
    assert.equal(calls[0].path, `/api/v1/ledgers/${ledger}/operations/${operationId}/corrections`);
    assert.equal(calls[0].path, calls[1].path);
    assert.equal(calls[0].init.body, calls[1].init.body);
    assert.equal(
        calls[0].init.headers["Idempotency-Key"],
        calls[1].init.headers["Idempotency-Key"],
    );
    const sent = JSON.parse(calls[0].init.body);
    assert.equal(sent.expected_version, 1);
    assert.equal("id" in sent, false);
    assert.equal("id" in sent.replacement, false);
    assert.equal(snapshot.command.operationId, operationId);
    assert.notEqual(snapshot.command.key, operationId);
});

test("cancellation keeps the existing identity and validates the exact next terminal revision", async (t) => {
    let pathSeen;
    let bodySeen;
    t.mock.method(globalThis, "fetch", async (path, init) => {
        pathSeen = path;
        bodySeen = JSON.parse(init.body);
        return Response.json(
            stateAt(
                {
                    action: "cancel",
                    operationId,
                    ledgerId: ledger,
                    kind: "expense",
                    bodyJson: init.body,
                },
                5,
                "cancelled",
            ),
        );
    });
    const controller = new PendingCommandController();
    controller.setOwner(session.user.id);
    await controller.cancel(session, ledger, operationId, "expense", {
        expected_version: 4,
        reason: "Fictional cancellation",
    });
    assert.equal(pathSeen, `/api/v1/ledgers/${ledger}/operations/${operationId}/cancellations`);
    assert.deepEqual(bodySeen, { expected_version: 4, reason: "Fictional cancellation" });
    assert.equal(controller.getSnapshot().status, "confirmed");
    assert.equal(controller.getSnapshot().revisionReceipt.version, 5);
    assert.equal(controller.getSnapshot().revisionReceipt.latest_posting.version, 4);
});

test("a wrong revision version, action result or existing ID cannot be accepted as this receipt", async () => {
    for (const [action, version, status, wrongId] of [
        ["correct", 1, "active", false],
        ["correct", 2, "cancelled", false],
        ["cancel", 2, "active", false],
        ["cancel", 3, "cancelled", false],
        ["correct", 2, "active", true],
    ]) {
        const controller = controllerWith({
            post: async (_session, command) => ({
                ...stateAt(command, version, status),
                ...(wrongId ? { id: "another-operation" } : {}),
            }),
        });
        if (action === "correct")
            await controller.correct(session, ledger, operationId, correction());
        else
            await controller.cancel(session, ledger, operationId, "expense", {
                expected_version: 1,
                reason: "Fictional cancellation",
            });
        assert.equal(controller.getSnapshot().status, "unknown");
        assert.equal(controller.getSnapshot().revisionReceipt, null);
        assert.equal(controller.dismiss(), false);
    }
});

test("only an explicit version_conflict followed by a later observed version unlocks a failed revision", async () => {
    let posts = 0;
    const controller = controllerWith({
        post: async (_session, command) => {
            posts += 1;
            if (posts === 1) throw new ApiError("network");
            if (posts === 2) throw new ApiError("server", 409, "version_conflict");
            return stateAt(command, command.expectedVersion + 1);
        },
        read: async (_session, command) => stateAt(command, 2),
    });
    await controller.correct(session, ledger, operationId, correction());
    const first = controller.getSnapshot().command;
    await controller.retry(session);
    assert.equal(controller.getSnapshot().status, "rejected");
    assert.equal(controller.getSnapshot().error.code, "version_conflict");
    assert.equal(controller.getSnapshot().current.version, 2);
    assert.equal(controller.getSnapshot().revisionReceipt, null);
    assert.equal(controller.dismiss(), true);
    await controller.correct(session, ledger, operationId, correction("expense", 2));
    assert.equal(controller.getSnapshot().status, "confirmed");
    assert.notEqual(controller.getSnapshot().command.key, first.key);
    assert.equal(controller.getSnapshot().command.operationId, first.operationId);
});

test("version_conflict with unchanged version, 404 or mismatched identity remains locked", async () => {
    for (const mode of ["same", "missing", "wrong-id"]) {
        let later = false;
        const controller = controllerWith({
            post: async () => {
                throw new ApiError("server", 409, "version_conflict");
            },
            read: async (_session, command) => {
                if (later) return stateAt(command, 2);
                if (mode === "missing") throw new ApiError("server", 404, "not_found");
                const state = stateAt(command, mode === "same" ? 1 : 2);
                return mode === "wrong-id" ? { ...state, id: "another-operation" } : state;
            },
        });
        await controller.correct(session, ledger, operationId, correction());
        assert.equal(controller.getSnapshot().status, "unknown");
        assert.equal(controller.dismiss(), false);
        later = true;
        await controller.reconcile(session);
        assert.equal(controller.getSnapshot().status, "rejected");
        assert.equal(controller.getSnapshot().error.code, "version_conflict");
    }
});

test("generic 409, archived entity, incorrect HTTP status and GET errors never supply conflict proof", async () => {
    for (const error of [
        new ApiError("server", 409),
        new ApiError("server", 409, "entity_archived"),
        new ApiError("server", 500, "version_conflict"),
    ]) {
        let posts = 0;
        const controller = controllerWith({
            post: async () => {
                if (++posts === 1) throw new ApiError("network");
                throw error;
            },
            read: async (_session, command) => stateAt(command, 2),
        });
        await controller.correct(session, ledger, operationId, correction());
        await controller.retry(session);
        await controller.reconcile(session);
        assert.equal(controller.getSnapshot().status, "unknown");
        assert.equal(controller.dismiss(), false);
    }
    const controller = controllerWith({
        post: async () => {
            throw new ApiError("network");
        },
        read: async () => {
            throw new ApiError("server", 409, "version_conflict");
        },
    });
    await controller.correct(session, ledger, operationId, correction());
    await controller.reconcile(session);
    assert.equal(controller.dismiss(), false);
});

test("same-owner authentication recovery retains revision action and exact payload without automatic replay", async () => {
    let posts = 0;
    const controller = controllerWith({
        post: async (_session, command) => {
            if (++posts === 1) throw new ApiError("unauthorized", 401);
            return stateAt(command, 2);
        },
    });
    await controller.correct(session, ledger, operationId, correction());
    const command = controller.getSnapshot().command;
    controller.setOwner(null);
    assert.equal(controller.getSnapshot().command, null);
    assert.equal(controller.getSnapshot().revisionReceipt, null);
    controller.setOwner(session.user.id);
    assert.equal(controller.getSnapshot().status, "unknown");
    assert.equal(controller.getSnapshot().command, command);
    assert.equal(posts, 1);
    await controller.retry({ ...session, csrf_token: "fictional-csrf-new" });
    assert.equal(controller.getSnapshot().status, "confirmed");
});

test("correction and cancellation share one pending slot, and logout invalidates conflict observations", async () => {
    const read = deferred();
    let observed;
    let posts = 0;
    const controller = controllerWith({
        post: async () => {
            posts += 1;
            throw new ApiError("server", 409, "version_conflict");
        },
        read: async (_session, command) => {
            observed = command;
            return read.promise;
        },
    });
    const first = controller.correct(session, ledger, operationId, correction());
    const second = controller.cancel(session, ledger, operationId, "expense", {
        expected_version: 1,
        reason: "Fictional cancellation",
    });
    assert.equal(first, second);
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(posts, 1);
    controller.setOwner(null, true);
    read.resolve(stateAt(observed, 2));
    await first;
    assert.equal(controller.getSnapshot().status, "idle");
    assert.equal(controller.getSnapshot().current, null);
});

test("whitelist prefill retains exact amounts, full fee/split data and immutable opening identity", () => {
    for (const kind of ["opening", "income", "expense", "transfer", "exchange"]) {
        const command = {
            action: "create",
            kind,
            operationId,
            ledgerId: ledger,
            bodyJson: JSON.stringify(input(kind).body),
        };
        const state = stateAt(command, 1);
        const posting = state.latest_posting;
        if (kind !== "opening")
            posting.fees = [
                {
                    account_id: "fee-account",
                    asset_id: "ETH",
                    amount: "0.000000000000000001",
                    category_id: "fee-category",
                    id: "forbidden-fee-metadata",
                },
            ];
        const prepared = postingInputFromState(state);
        const replacement = replacementFromInput({
            ...prepared,
            body: {
                ...prepared.body,
                id: "forbidden-id",
                journal_id: "forbidden-journal",
                version: 999,
            },
        });
        assert.equal(replacement.kind, kind);
        for (const field of [
            "id",
            "ledger_id",
            "journal_id",
            "version",
            "created_at",
            "updated_at",
        ])
            assert.equal(field in replacement, false);
        assert.equal(replacement.transaction_date, posting.transaction_date);
        if (kind === "opening") {
            assert.equal(replacement.account_id, posting.account_id);
            assert.equal(replacement.asset_id, posting.asset_id);
            assert.equal("fees" in replacement, false);
            assert.equal("recognition_date" in replacement, false);
        } else {
            assert.deepEqual(replacement.fees, [
                {
                    account_id: "fee-account",
                    asset_id: "ETH",
                    amount: "0.000000000000000001",
                    category_id: "fee-category",
                },
            ]);
            posting.fees[0].amount = "1";
            assert.equal(prepared.body.fees[0].amount, "0.000000000000000001");
            assert.equal(replacement.fees[0].amount, "0.000000000000000001");
        }
        if (["income", "expense"].includes(kind)) {
            posting.splits[0].amount = "999";
            assert.equal(replacement.splits[0].amount, "1.00");
            assert.equal(replacement.recognition_date, "2026-01-01");
        }
        if (["transfer", "exchange"].includes(kind))
            assert.equal("recognition_date" in replacement, false);
    }
});

test("history reads are paginated and keep raw signed audit quantities", async (t) => {
    let pathSeen;
    const history = [
        {
            version: 2,
            action: "correct",
            actor_id: "owner",
            reason: "Fictional correction",
            recorded_at: "2026-01-04T00:00:00Z",
            journals: [{ lines: [{ amount: "-0.000000000000000001", component_no: 1 }] }],
        },
    ];
    t.mock.method(globalThis, "fetch", async (path) => {
        pathSeen = path;
        return Response.json(history);
    });
    const result = await getOperationHistory(ledger, operationId, { limit: 10, offset: 20 });
    assert.equal(
        pathSeen,
        `/api/v1/ledgers/${ledger}/operations/${operationId}/history?limit=10&offset=20`,
    );
    assert.equal(result[0].journals[0].lines[0].amount, "-0.000000000000000001");
});
