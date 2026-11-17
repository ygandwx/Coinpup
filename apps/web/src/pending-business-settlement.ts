import { correctReturn, cancelReturn } from "./allocation-return-revision-api";
import { validReturnRevisionReview } from "./allocation-return-revision-intent";
import type { ReturnRevisionInput } from "./allocation-return-revision-intent";
import { validRefundRevisionReview } from "./refund-revision-intent";
import type { RefundRevisionInput } from "./refund-revision-intent";
import { correctRefund, cancelRefund, createRefund } from "./refund-api";
import { validRefundReview } from "./refund-intent";
import { ApiError } from "./api";
import type { Session } from "./api";
import { createBusinessAllocation, createBusinessPayment } from "./business-settlement-api";
import { createAllocationReturn } from "./allocation-return-api";
import { validReturnReview } from "./allocation-return-intent";
import {
    freezeSettlementReceipt,
    validSettlementInput,
    validSettlementReceipt,
} from "./settlement-intent";
import type {
    DeepReadonly,
    FrozenSettlement,
    SettlementInput,
    SettlementReceipt,
} from "./settlement-intent";
import type { PendingStatus } from "./pending-command";

export type BusinessSettlementSnapshot = Readonly<{
    status: PendingStatus;
    plan: FrozenSettlement | null;
    receipt: DeepReadonly<SettlementReceipt> | null;
    error: ApiError | null;
}>;
type Transport = (session: Session, plan: FrozenSettlement) => Promise<unknown>;
const transport: Transport = (session, plan) => {
    const body = JSON.parse(plan.bodyJson);
    if (plan.kind === "return-revision") {
        const submit = plan.action === "correct" ? correctReturn : cancelReturn;
        return submit(session.csrf_token, plan.ledgerId, plan.returnId, body, plan.key);
    }
    if (plan.kind === "refund-revision") {
        const submit = plan.action === "correct" ? correctRefund : cancelRefund;
        return submit(session.csrf_token, plan.ledgerId, plan.refundId, body, plan.key);
    }
    const submit = {
        payment: createBusinessPayment,
        return: createAllocationReturn,
        refund: createRefund,
        allocation: createBusinessAllocation,
    }[plan.kind];
    return submit(session.csrf_token, plan.ledgerId, body, plan.key);
};
const idle = (): BusinessSettlementSnapshot =>
    Object.freeze({ status: "idle", plan: null, receipt: null, error: null });

export class PendingBusinessSettlementController {
    private state = idle();
    private visible = this.state;
    private owner: string | null = null;
    private generation = 0;
    private flight: Promise<void> | null = null;
    private uncertain = false;
    private listeners = new Set<() => void>();
    private io: Transport;
    constructor(io: Transport = transport) {
        this.io = io;
    }
    getSnapshot = (): BusinessSettlementSnapshot => this.visible;
    subscribe = (listener: () => void): (() => void) => {
        this.listeners.add(listener);
        return () => this.listeners.delete(listener);
    };
    private publish(next = this.state): void {
        this.state = Object.freeze(next);
        this.visible =
            this.owner === null && next.plan
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
            (ownerId !== null && this.state.plan && ownerId !== this.state.plan.ownerId)
        )
            this.reset();
        if (
            ownerId !== null &&
            ownerId === this.state.plan?.ownerId &&
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
            (this.state.plan && this.state.plan.ownerId !== this.owner)
        )
            throw new ApiError("unauthorized", 401, "settlement_owner_changed");
    }
    start(
        session: Session,
        ledgerId: string,
        input:
            | SettlementInput
            | { kind: "refund-revision"; revision: RefundRevisionInput }
            | { kind: "return-revision"; revision: ReturnRevisionInput },
        reviewed?: unknown,
    ): Promise<void> {
        try {
            this.authorized(session);
            if (this.flight || !["idle", "confirmed", "rejected"].includes(this.state.status))
                throw new ApiError("server", null, "pending_settlement_unresolved");
            let valid: boolean;
            switch (input.kind) {
                case "return-revision":
                    valid = validReturnRevisionReview(ledgerId, input.revision, reviewed);
                    break;
                case "refund-revision":
                    valid = validRefundRevisionReview(ledgerId, input.revision, reviewed);
                    break;
                default:
                    valid =
                        validSettlementInput(ledgerId, input) &&
                        (input.kind !== "return" ||
                            validReturnReview(ledgerId, input.body, reviewed)) &&
                        (input.kind !== "refund" ||
                            validRefundReview(ledgerId, input.body, reviewed));
            }
            if (!valid) throw new ApiError("server", null, "invalid_settlement_intent");
            const body = "revision" in input ? input.revision.body : input.body;
            const bodyJson = JSON.stringify(body),
                reviewedJson = JSON.stringify(reviewed === undefined ? body : reviewed);
            if (typeof reviewedJson !== "string") throw new Error("Invalid review snapshot");
            const common = {
                ownerId: this.owner!,
                ledgerId,
                key: crypto.randomUUID(),
                bodyJson,
                reviewedJson,
            };
            const plan: FrozenSettlement =
                input.kind === "return-revision"
                    ? {
                          ...common,
                          kind: input.kind,
                          action: input.revision.action,
                          returnId: input.revision.returnId,
                      }
                    : input.kind === "refund-revision"
                      ? {
                            ...common,
                            kind: input.kind,
                            action: input.revision.action,
                            refundId: input.revision.refundId,
                        }
                      : { ...common, kind: input.kind };
            this.uncertain = false;
            this.state = Object.freeze({
                ...idle(),
                plan: Object.freeze(plan),
            });
            return this.send(session);
        } catch (error) {
            return Promise.reject(
                error instanceof ApiError
                    ? error
                    : new ApiError("server", null, "invalid_settlement_intent"),
            );
        }
    }
    retry(session: Session): Promise<void> {
        try {
            this.authorized(session);
            if (
                this.flight ||
                !this.state.plan ||
                !["unknown", "auth-required"].includes(this.state.status)
            )
                throw new ApiError("server", null, "settlement_retry_unavailable");
            return this.send(session);
        } catch (error) {
            return Promise.reject(error);
        }
    }
    private send(session: Session): Promise<void> {
        const generation = this.generation,
            plan = this.state.plan!;
        const flight = Promise.resolve().then(async () => {
            try {
                if (generation !== this.generation) return;
                if (this.owner !== String(session.user.id)) {
                    this.publish({ ...this.state, status: "auth-required" });
                    return;
                }
                const result = await this.io(session, plan);
                if (generation !== this.generation) return;
                if (!validSettlementReceipt(result, plan))
                    throw new ApiError("server", "action" in plan ? 200 : 201, "invalid_response");
                this.uncertain = false;
                this.publish({
                    ...this.state,
                    status: "confirmed",
                    receipt: freezeSettlementReceipt(result),
                    error: null,
                });
            } catch (caught) {
                if (generation !== this.generation) return;
                const error = caught instanceof ApiError ? caught : new ApiError("network");
                let status: PendingStatus;
                if (error.kind === "unauthorized") status = "auth-required";
                else if (error.code === "idempotency_conflict") status = "idempotency-conflict";
                else if (
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
        // Set the flight before notifying listeners, including re-entrant submit attempts.
        this.flight = flight;
        this.publish({ ...this.state, status: "submitting", error: null });
        return flight;
    }
}
