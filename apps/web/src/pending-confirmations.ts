import { ApiError } from "./api";
import type { Session } from "./api";
import { confirmDraft } from "./ocr-api";
import type { ConfirmationCreate, ConfirmationReceipt } from "./ocr-api";
import { validReceipt } from "./pending-command";
import type { PendingStatus } from "./pending-command";

export type ConfirmationInput = Readonly<{
    ledgerId: string;
    draftId: string;
    body: Omit<ConfirmationCreate, "intent_id">;
}>;
export type FrozenConfirmation = Readonly<{
    ownerId: string;
    ledgerId: string;
    draftId: string;
    bodyJson: string;
}>;
export type ConfirmationSnapshot = Readonly<{
    status: PendingStatus;
    plans: readonly FrozenConfirmation[];
    index: number;
    receipts: readonly ConfirmationReceipt[];
    error: ApiError | null;
}>;
type Transport = (session: Session, plan: FrozenConfirmation) => Promise<unknown>;
const transport: Transport = (session, plan) =>
    confirmDraft(session.csrf_token, plan.ledgerId, plan.draftId, JSON.parse(plan.bodyJson));
const idle = (): ConfirmationSnapshot =>
    Object.freeze({
        status: "idle",
        plans: Object.freeze([]),
        index: 0,
        receipts: Object.freeze([]),
        error: null,
    });
function record(value: unknown): value is Record<string, unknown> {
    return value !== null && typeof value === "object" && !Array.isArray(value);
}
function valid(value: unknown, plan: FrozenConfirmation): value is ConfirmationReceipt {
    const body = JSON.parse(plan.bodyJson) as ConfirmationCreate;
    if (
        !record(value) ||
        value.intent_id !== body.intent_id ||
        value.draft_id !== plan.draftId ||
        value.ledger_id !== plan.ledgerId ||
        value.draft_version !== body.expected_version + 1 ||
        value.target_ledger_id !== body.target_ledger_id ||
        value.target_file_id !== body.target_file_id ||
        value.action !== (body.entry.kind === "link" ? "link" : "create") ||
        !record(value.operation)
    )
        return false;
    const operation = value.operation;
    const kind = body.entry.kind === "link" ? operation.kind : body.entry.kind;
    if (
        kind !== "opening" &&
        kind !== "income" &&
        kind !== "expense" &&
        kind !== "transfer" &&
        kind !== "exchange"
    )
        return false;
    const id = body.entry.kind === "link" ? body.entry.operation_id : body.entry.command.id;
    const version = body.entry.kind === "link" ? body.entry.expected_version : 1;
    return (
        typeof id === "string" &&
        operation.version === version &&
        validReceipt(operation, { operationId: id, ledgerId: body.target_ledger_id, kind })
    );
}

/** Per-App memory only. A batch never passes an unresolved row or silently changes its intent. */
export class PendingConfirmationController {
    private owner: string | null = null;
    private state = idle();
    private visible = this.state;
    private generation = 0;
    private uncertain = false;
    private flight: Promise<void> | null = null;
    private listeners = new Set<() => void>();
    private io: Transport;
    constructor(io: Transport = transport) {
        this.io = io;
    }
    getSnapshot = (): ConfirmationSnapshot => this.visible;
    subscribe = (listener: () => void): (() => void) => {
        this.listeners.add(listener);
        return () => this.listeners.delete(listener);
    };
    private publish(next = this.state): void {
        this.state = Object.freeze(next);
        this.visible =
            this.owner === null && next.plans.length
                ? Object.freeze({ ...idle(), status: "auth-required" })
                : this.state;
        for (const listener of [...this.listeners]) listener();
    }
    private reset(): void {
        this.generation += 1;
        this.flight = null;
        this.uncertain = false;
        this.state = idle();
    }
    setOwner(ownerId: string | null, explicitLogout = false): void {
        if (!explicitLogout && ownerId === this.owner) return;
        if (
            explicitLogout ||
            (ownerId !== null && this.state.plans.length && ownerId !== this.state.plans[0].ownerId)
        )
            this.reset();
        if (
            ownerId !== null &&
            ownerId === this.state.plans[0]?.ownerId &&
            this.state.status === "auth-required"
        ) {
            this.uncertain = true;
            this.state = { ...this.state, status: "unknown", error: null };
        }
        this.owner = ownerId;
        this.publish();
    }
    dismiss = (): boolean => {
        if (!["idle", "confirmed", "rejected"].includes(this.state.status)) return false;
        this.reset();
        this.publish();
        return true;
    };
    private authorized(session: Session): void {
        if (
            this.owner !== String(session.user.id) ||
            (this.state.plans.length && this.state.plans[0].ownerId !== this.owner)
        )
            throw new ApiError("unauthorized", 401, "confirmation_owner_changed");
    }
    start(session: Session, inputs: readonly ConfirmationInput[]): Promise<void> {
        try {
            this.authorized(session);
            if (this.flight || !["idle", "confirmed", "rejected"].includes(this.state.status))
                throw new ApiError("server", null, "pending_confirmation_unresolved");
            if (
                !inputs.length ||
                inputs.length > 200 ||
                new Set(inputs.map((p) => `${p.ledgerId}/${p.draftId}`)).size !== inputs.length
            )
                throw new ApiError("server", null, "invalid_confirmation_batch");
            const plans = inputs.map((input) => {
                const body = JSON.parse(JSON.stringify(input.body)) as ConfirmationCreate;
                if (
                    !input.ledgerId ||
                    !input.draftId ||
                    !body.target_ledger_id ||
                    !body.target_file_id ||
                    !Number.isSafeInteger(body.expected_version) ||
                    body.expected_version < 1
                )
                    throw new ApiError("server", null, "invalid_confirmation_batch");
                body.intent_id = crypto.randomUUID();
                if (body.entry.kind !== "link") body.entry.command.id ??= crypto.randomUUID();
                return Object.freeze({
                    ownerId: String(session.user.id),
                    ledgerId: input.ledgerId,
                    draftId: input.draftId,
                    bodyJson: JSON.stringify(body),
                });
            });
            this.uncertain = false;
            this.publish({ ...idle(), plans: Object.freeze(plans), status: "submitting" });
            return this.send(session);
        } catch (error) {
            return Promise.reject(error);
        }
    }
    retry(session: Session): Promise<void> {
        try {
            this.authorized(session);
            if (this.flight) return this.flight;
            if (
                !["unknown", "auth-required"].includes(this.state.status) ||
                !this.state.plans.length
            )
                throw new ApiError("server", null, "confirmation_retry_unavailable");
            return this.send(session);
        } catch (error) {
            return Promise.reject(error);
        }
    }
    private send(session: Session): Promise<void> {
        const generation = this.generation;
        this.publish({ ...this.state, status: "submitting", error: null });
        const flight = Promise.resolve().then(async () => {
            try {
                while (this.state.index < this.state.plans.length) {
                    if (generation !== this.generation) return;
                    if (this.owner !== String(session.user.id)) {
                        this.publish({ ...this.state, status: "auth-required" });
                        return;
                    }
                    const plan = this.state.plans[this.state.index];
                    const receipt = await this.io(session, plan);
                    if (generation !== this.generation) return;
                    if (!valid(receipt, plan))
                        throw new ApiError("server", 200, "invalid_response");
                    this.uncertain = false;
                    this.publish({
                        ...this.state,
                        index: this.state.index + 1,
                        receipts: Object.freeze([...this.state.receipts, receipt]),
                    });
                }
                this.publish({ ...this.state, status: "confirmed" });
            } catch (caught) {
                if (generation !== this.generation) return;
                const error = caught instanceof ApiError ? caught : new ApiError("network");
                let status: PendingStatus;
                if (error.kind === "unauthorized") status = "auth-required";
                else if (error.code === "idempotency_conflict") status = "idempotency-conflict";
                else if (error.code === "version_conflict") {
                    // The server checked this exact intent first. Monotonic draft versions
                    // prevent any late copy of the original request from committing later.
                    status = "rejected";
                    this.uncertain = false;
                } else if (
                    !this.uncertain &&
                    error.status !== null &&
                    [400, 403, 404, 409, 422, 429].includes(error.status)
                )
                    status = "rejected";
                else {
                    status = "unknown";
                    this.uncertain = true;
                }
                this.publish({ ...this.state, status, error });
            } finally {
                if (generation === this.generation) this.flight = null;
            }
        });
        this.flight = flight;
        return flight;
    }
}
