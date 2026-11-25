import { ApiError } from "./api";
import type { Session } from "./api";
export type MasterStatus =
    "idle" | "submitting" | "unknown" | "auth-required" | "confirmed" | "conflict" | "rejected";
export type MasterIdentity = Readonly<{
    ownerId: string;
    ledgerId: string;
    id: string;
    bodyJson: string;
}>;
export type VersionedSnapshot<P, R> = Readonly<{
    status: MasterStatus;
    plan: P | null;
    record: R | null;
    error: ApiError | null;
}>;
type ReplyPolicy<P, R> = {
    send: (s: Session, p: P) => Promise<unknown>;
    scoped: (v: unknown, p: P) => v is R;
    matches: (v: R, p: P) => boolean;
    capture: (v: R) => R;
};
export type MasterPolicy<P, R> = ReplyPolicy<P, R> & {
    read: (p: P) => Promise<unknown>;
    create: (p: P) => boolean;
    changed?: (value: R, plan: P) => boolean;
};
const terminal = ["idle", "confirmed", "conflict", "rejected"];
const idle = <P, R>(): VersionedSnapshot<P, R> =>
    Object.freeze({ status: "idle", plan: null, record: null, error: null });
export class VersionedMasterController<P extends MasterIdentity, R extends object> {
    private owner: string | null = null;
    private state: VersionedSnapshot<P, R> = idle();
    private visible = this.state;
    private generation = 0;
    private uncertain = false;
    private flight: Promise<void> | null = null;
    private listeners = new Set<() => void>();
    private policy: ReplyPolicy<P, R> | MasterPolicy<P, R>;
    constructor(policy: ReplyPolicy<P, R> | MasterPolicy<P, R>) {
        this.policy = policy;
    }
    getSnapshot = (): VersionedSnapshot<P, R> => this.visible;
    subscribe = (listener: () => void): (() => void) => {
        this.listeners.add(listener);
        return () => this.listeners.delete(listener);
    };
    private publish(next = this.state): void {
        this.state = Object.freeze(next);
        this.visible =
            (this.owner === null && next.plan) || next.status === "auth-required"
                ? Object.freeze({ ...idle<P, R>(), status: "auth-required" })
                : this.state;
        for (const listener of [...this.listeners]) listener();
    }
    private reset(): void {
        this.generation += 1;
        this.flight = null;
        this.uncertain = false;
        this.state = idle<P, R>();
    }
    setOwner(ownerId: string | null, explicitLogout = false): void {
        if (!explicitLogout && ownerId === this.owner) return;
        if (
            ownerId === null &&
            !explicitLogout &&
            this.state.plan &&
            !terminal.includes(this.state.status)
        ) {
            this.generation++;
            this.flight = null;
            this.uncertain = true;
            this.state = { ...this.state, status: "auth-required", error: null };
        }

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
        if (!terminal.includes(this.state.status) || this.flight) return false;
        this.reset();
        this.publish();
        return true;
    };
    private authorized(session: Session): void {
        if (
            this.owner !== String(session.user.id) ||
            (this.state.plan && this.state.plan.ownerId !== this.owner)
        )
            throw new ApiError("unauthorized", 401, "master_owner_changed");
    }
    protected begin(session: Session, prepare: () => P): Promise<void> {
        try {
            this.authorized(session);
            if (this.flight || !terminal.includes(this.state.status))
                throw new ApiError("server", null, "pending_master_unresolved");
            const plan = prepare();
            if (plan.ownerId !== this.owner)
                throw new ApiError("unauthorized", 401, "master_owner_changed");
            this.uncertain = false;
            this.state = { ...idle<P, R>(), plan: Object.freeze(plan) };
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
                throw new ApiError("server", null, "master_retry_unavailable");
            return this.send(session);
        } catch (error) {
            return Promise.reject(error);
        }
    }
    private send(session: Session): Promise<void> {
        const generation = this.generation,
            plan = this.state.plan!,
            policy = this.policy;
        const flight = Promise.resolve().then(async () => {
            try {
                if (generation !== this.generation) return;
                if (this.owner !== String(session.user.id)) {
                    this.publish({ ...this.state, status: "auth-required" });
                    return;
                }
                const value = await policy.send(session, plan);
                if (generation !== this.generation) return;
                if (!policy.scoped(value, plan) || !policy.matches(value, plan))
                    throw new ApiError("server", 200, "invalid_response");
                this.uncertain = false;
                this.publish({
                    ...this.state,
                    status: "confirmed",
                    record: policy.capture(value),
                });
            } catch (caught) {
                if (generation !== this.generation) return;
                let error = caught instanceof ApiError ? caught : new ApiError("network");
                let status: MasterStatus = "unknown";
                let record: R | null = null;
                if (error.kind === "unauthorized") status = "auth-required";
                else if (
                    "read" in policy &&
                    error.status === 409 &&
                    ((policy.create(plan) && error.code === "duplicate_record") ||
                        (!policy.create(plan) && error.code === "version_conflict"))
                ) {
                    try {
                        const current = await policy.read(plan);
                        if (generation !== this.generation) return;
                        if (!policy.scoped(current, plan))
                            throw new ApiError("server", 200, "invalid_response");
                        if (policy.create(plan))
                            status = policy.matches(current, plan) ? "confirmed" : "conflict";
                        else if (
                            !this.uncertain ||
                            (policy.changed?.(current, plan) ??
                                ("version" in current &&
                                    typeof current.version === "number" &&
                                    current.version > JSON.parse(plan.bodyJson).expected_version))
                        )
                            status = "conflict";
                        if (status !== "unknown") record = policy.capture(current);
                    } catch (readError) {
                        if (generation !== this.generation) return;
                        error = readError instanceof ApiError ? readError : new ApiError("network");
                        if (error.kind === "unauthorized") status = "auth-required";
                    }
                } else if (
                    !this.uncertain &&
                    error.status !== null &&
                    [400, 403, 404, 409, 422, 429].includes(error.status)
                )
                    status = "rejected";
                if (status === "auth-required") this.owner = null;
                this.uncertain = ["unknown", "auth-required"].includes(status);
                this.publish({
                    ...this.state,
                    status,
                    record,
                    error: status === "confirmed" ? null : error,
                });
            } finally {
                if (generation === this.generation) this.flight = null;
            }
        });
        this.flight = flight;
        this.publish({ ...this.state, status: "submitting", error: null });
        return flight;
    }
}
