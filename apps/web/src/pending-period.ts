import { ApiError } from "./api";
import type { Session } from "./api";
import type { PendingStatus } from "./pending-command";
import { changePeriod, getPeriod } from "./periods-api";
import type { PeriodChange, PeriodReceipt } from "./periods-api";

export type FrozenPeriod = Readonly<{
    ownerId: string;
    ledgerId: string;
    key: string;
    bodyJson: string;
}>;
export type PeriodSnapshot = Readonly<{
    status: PendingStatus;
    plan: FrozenPeriod | null;
    receipt: PeriodReceipt | null;
    error: ApiError | null;
}>;
type Transport = (session: Session, plan: FrozenPeriod) => Promise<unknown>;
const transport: Transport = (session, plan) =>
    changePeriod(session.csrf_token, plan.ledgerId, JSON.parse(plan.bodyJson), plan.key);
const idle = (): PeriodSnapshot =>
    Object.freeze({ status: "idle", plan: null, receipt: null, error: null });
function date(value: unknown): value is string {
    return (
        typeof value === "string" &&
        /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/u.test(value) &&
        value.slice(0, 4) !== "0000" &&
        Number.isFinite(Date.parse(`${value}T00:00:00Z`)) &&
        new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value
    );
}
function validInput(body: PeriodChange): boolean {
    return (
        ["close", "reopen"].includes(body.action) &&
        Number.isInteger(body.expected_version) &&
        body.expected_version > 0 &&
        body.expected_version <= 2147483646 &&
        (date(body.closed_through) || (body.action === "reopen" && body.closed_through === null)) &&
        typeof body.reason === "string" &&
        body.reason.trim().length > 0 &&
        body.reason.length <= 1000 &&
        !body.reason.includes("\0")
    );
}
function valid(value: unknown, plan: FrozenPeriod): value is PeriodReceipt {
    if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
    const receipt = value as Record<string, unknown>;
    const body = JSON.parse(plan.bodyJson) as PeriodChange;
    return (
        typeof receipt.id === "string" &&
        receipt.id.length > 0 &&
        receipt.ledger_id === plan.ledgerId &&
        receipt.actor_id === plan.ownerId &&
        receipt.version === body.expected_version + 1 &&
        receipt.action === body.action &&
        receipt.closed_through === body.closed_through &&
        receipt.reason === body.reason.trim() &&
        (receipt.previous_closed_through === null || date(receipt.previous_closed_through)) &&
        typeof receipt.created_at === "string" &&
        Number.isFinite(Date.parse(receipt.created_at))
    );
}

/** One in-memory intent; transport uncertainty never refreshes its version, body or key. */
export class PendingPeriodController {
    private owner: string | null = null;
    private state = idle();
    private visible = this.state;
    private generation = 0;
    private uncertain = false;
    private flight: Promise<void> | null = null;
    private listeners = new Set<() => void>();
    private io: Transport;
    private read: (ledgerId: string) => Promise<unknown>;
    constructor(
        io: Transport = transport,
        read = getPeriod as (ledgerId: string) => Promise<unknown>,
    ) {
        this.io = io;
        this.read = read;
    }
    getSnapshot = (): PeriodSnapshot => this.visible;
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
            throw new ApiError("unauthorized", 401, "period_owner_changed");
    }
    start(session: Session, ledgerId: string, body: PeriodChange): Promise<void> {
        try {
            this.authorized(session);
            if (this.flight || !["idle", "confirmed", "rejected"].includes(this.state.status))
                throw new ApiError("server", null, "pending_period_unresolved");
            if (typeof ledgerId !== "string" || !ledgerId || !validInput(body))
                throw new ApiError("server", null, "invalid_period_intent");
            this.uncertain = false;
            this.publish({
                ...idle(),
                plan: Object.freeze({
                    ownerId: this.owner!,
                    ledgerId,
                    key: crypto.randomUUID(),
                    bodyJson: JSON.stringify(body),
                }),
            });
            return this.send(session);
        } catch (error) {
            return Promise.reject(error);
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
                throw new ApiError("server", null, "period_retry_unavailable");
            return this.send(session);
        } catch (error) {
            return Promise.reject(error);
        }
    }
    private send(session: Session): Promise<void> {
        const generation = this.generation;
        const plan = this.state.plan!;
        this.publish({ ...this.state, status: "submitting", error: null });
        const flight = Promise.resolve().then(async () => {
            try {
                if (generation !== this.generation) return;
                if (this.owner !== String(session.user.id)) {
                    this.publish({ ...this.state, status: "auth-required" });
                    return;
                }
                const result = await this.io(session, plan);
                if (generation !== this.generation) return;
                if (!valid(result, plan)) throw new ApiError("server", 200, "invalid_response");
                this.uncertain = false;
                this.publish({
                    ...this.state,
                    status: "confirmed",
                    receipt: Object.freeze({ ...result }),
                });
            } catch (caught) {
                if (generation !== this.generation) return;
                const error = caught instanceof ApiError ? caught : new ApiError("network");
                let status: PendingStatus;
                if (error.kind === "unauthorized") status = "auth-required";
                else if (error.code === "idempotency_conflict") status = "idempotency-conflict";
                else if (
                    error.status === 409 &&
                    error.code === "version_conflict" &&
                    this.uncertain
                ) {
                    // A higher observed version plus receipt-first rejection fences every late copy.
                    status = "unknown";
                    try {
                        const current = await this.read(plan.ledgerId);
                        if (generation !== this.generation) return;
                        const observed = current as Record<string, unknown> | null;
                        if (
                            observed?.ledger_id === plan.ledgerId &&
                            Number.isInteger(observed.version) &&
                            (observed.version as number) >
                                JSON.parse(plan.bodyJson).expected_version
                        ) {
                            status = "rejected";
                            this.uncertain = false;
                        }
                    } catch (readError) {
                        if (generation !== this.generation) return;
                        if (readError instanceof ApiError && readError.kind === "unauthorized")
                            status = "auth-required";
                    }
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
