import { ApiError } from "./api";
import type { Session } from "./api";
import { cancelCredit, correctCredit, createCredit } from "./credit-api";
import type {
    CreditCancellation,
    CreditCorrection,
    CreditCreate,
    CreditReceipt,
    CreditRevisionReceipt,
} from "./credit-api";
import {
    validCreditReview,
    validCreditReceipt,
    freezeCreditReceipt,
    freezeCreditValue,
} from "./credit-intent";
import type { FrozenCredit } from "./credit-intent";
import { isCreditRevisionPlan, validCreditRevisionReview } from "./credit-revision-intent";
import type { CreditRevisionAction, FrozenCreditRevision } from "./credit-revision-intent";
import { validCreditRevisionReceipt } from "./credit-revision-receipt";
import type { DeepReadonly } from "./settlement-intent";
import type { PendingStatus } from "./pending-command";
export type BusinessCreditSnapshot = Readonly<{
    status: PendingStatus;
    plan: FrozenCredit | FrozenCreditRevision | null;
    receipt: DeepReadonly<CreditReceipt> | null;
    revisionReceipt: DeepReadonly<CreditRevisionReceipt> | null;
    error: ApiError | null;
}>;
type Transport = (session: Session, plan: FrozenCredit) => Promise<unknown>;
const transport: Transport = (session, plan) => {
    if (isCreditRevisionPlan(plan))
        return (plan.action === "correct" ? correctCredit : cancelCredit)(
            session.csrf_token,
            plan.ledgerId,
            plan.creditId,
            JSON.parse(plan.bodyJson),
            plan.key,
        );
    return createCredit(session.csrf_token, plan.ledgerId, JSON.parse(plan.bodyJson), plan.key);
};
const idle = (): BusinessCreditSnapshot =>
    Object.freeze({
        status: "idle",
        plan: null,
        receipt: null,
        revisionReceipt: null,
        error: null,
    });

export class PendingBusinessCreditController {
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
    getSnapshot = (): BusinessCreditSnapshot => this.visible;
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
            throw new ApiError("unauthorized", 401, "credit_owner_changed");
    }
    start(
        session: Session,
        ledgerId: string,
        input: CreditCreate,
        reviewed?: unknown,
    ): Promise<void> {
        try {
            this.authorized(session);
            if (this.flight || !["idle", "confirmed", "rejected"].includes(this.state.status))
                throw new ApiError("server", null, "pending_credit_unresolved");
            if (!validCreditReview(ledgerId, input, reviewed))
                throw new ApiError("server", null, "invalid_credit_intent");
            const bodyJson = JSON.stringify(input),
                reviewedJson = JSON.stringify(reviewed);
            if (typeof reviewedJson !== "string") throw new Error("Invalid review snapshot");
            this.uncertain = false;
            this.state = Object.freeze({
                ...idle(),
                plan: Object.freeze({
                    ownerId: this.owner!,
                    ledgerId,
                    key: crypto.randomUUID(),
                    bodyJson,
                    reviewedJson,
                }),
            });
            return this.send(session);
        } catch (error) {
            return Promise.reject(
                error instanceof ApiError
                    ? error
                    : new ApiError("server", null, "invalid_credit_intent"),
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
                throw new ApiError("server", null, "credit_retry_unavailable");
            return this.send(session);
        } catch (error) {
            return Promise.reject(error);
        }
    }
    startRevision(
        session: Session,
        ledgerId: string,
        creditId: string,
        action: CreditRevisionAction,
        input: CreditCorrection | CreditCancellation,
        reviewed?: unknown,
    ): Promise<void> {
        try {
            this.authorized(session);
            if (this.flight || !["idle", "confirmed", "rejected"].includes(this.state.status))
                throw new ApiError("server", null, "pending_credit_unresolved");
            if (!validCreditRevisionReview(ledgerId, creditId, action, input, reviewed))
                throw new ApiError("server", null, "invalid_credit_intent");
            const bodyJson = JSON.stringify(input),
                reviewedJson = JSON.stringify(reviewed);
            if (typeof reviewedJson !== "string") throw new Error("Invalid review snapshot");
            this.uncertain = false;
            this.state = Object.freeze({
                ...idle(),
                plan: Object.freeze({
                    ownerId: this.owner!,
                    ledgerId,
                    creditId,
                    action,
                    key: crypto.randomUUID(),
                    bodyJson,
                    reviewedJson,
                }),
            });
            return this.send(session);
        } catch (error) {
            return Promise.reject(
                error instanceof ApiError
                    ? error
                    : new ApiError("server", null, "invalid_credit_intent"),
            );
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
                let saved: Pick<BusinessCreditSnapshot, "receipt" | "revisionReceipt">;
                if (isCreditRevisionPlan(plan)) {
                    if (!validCreditRevisionReceipt(result, plan))
                        throw new ApiError("server", 200, "invalid_response");
                    saved = { receipt: null, revisionReceipt: freezeCreditValue(result) };
                } else {
                    if (!validCreditReceipt(result, plan))
                        throw new ApiError("server", 201, "invalid_response");
                    saved = { receipt: freezeCreditReceipt(result), revisionReceipt: null };
                }
                this.uncertain = false;
                this.publish({
                    ...this.state,
                    status: "confirmed",
                    ...saved,
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
