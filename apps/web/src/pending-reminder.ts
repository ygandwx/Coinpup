import { ApiError } from "./api";
import type { Session } from "./api";
import { getReminder, saveReminder } from "./reminder-api";
import type { Reminder, ReminderBody, ReminderAction } from "./reminder-api";
import { matchesReminder, scopedReminder } from "./reminder-intent";
import type { FrozenReminder } from "./reminder-intent";

export type ReminderStatus =
    "idle" | "submitting" | "unknown" | "auth-required" | "confirmed" | "conflict" | "rejected";
export type ReminderSnapshot = Readonly<{
    status: ReminderStatus;
    plan: FrozenReminder | null;
    record: Reminder | null;
    error: ApiError | null;
}>;
type Transport = (session: Session, plan: FrozenReminder) => Promise<unknown>;
type Reader = (plan: FrozenReminder) => Promise<unknown>;
const terminal = ["idle", "confirmed", "conflict", "rejected"];
const idle = (): ReminderSnapshot =>
    Object.freeze({ status: "idle", plan: null, record: null, error: null });
const capture = (value: Reminder): Reminder => Object.freeze(structuredClone(value));

export class PendingReminderController {
    private owner: string | null = null;
    private state = idle();
    private visible = this.state;
    private generation = 0;
    private uncertain = false;
    private flight: Promise<void> | null = null;
    private listeners = new Set<() => void>();
    private io: Transport;
    private read: Reader;
    constructor(
        io: Transport = (session, plan) =>
            saveReminder(
                session.csrf_token,
                plan.ledgerId,
                plan.id,
                plan.action,
                JSON.parse(plan.bodyJson),
            ),
        read: Reader = (plan) => getReminder(plan.ledgerId, plan.id),
    ) {
        this.io = io;
        this.read = read;
    }
    getSnapshot = (): ReminderSnapshot => this.visible;
    subscribe = (listener: () => void): (() => void) => {
        this.listeners.add(listener);
        return () => this.listeners.delete(listener);
    };
    private publish(next = this.state): void {
        this.state = Object.freeze(next);
        this.visible =
            (this.owner === null && next.plan) || next.status === "auth-required"
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
        if (!terminal.includes(this.state.status)) return false;
        this.reset();
        this.publish();
        return true;
    };
    private authorized(session: Session): void {
        if (
            this.owner !== String(session.user.id) ||
            (this.state.plan && this.state.plan.ownerId !== this.owner)
        )
            throw new ApiError("unauthorized", 401, "reminder_owner_changed");
    }
    start(
        session: Session,
        ledgerId: string,
        id: string,
        action: ReminderAction,
        body: ReminderBody,
    ): Promise<void> {
        try {
            this.authorized(session);
            if (this.flight || !terminal.includes(this.state.status))
                throw new ApiError("server", null, "pending_reminder_unresolved");
            const data = body as unknown as Record<string, unknown>;
            if (
                !body ||
                typeof body !== "object" ||
                Array.isArray(body) ||
                typeof ledgerId !== "string" ||
                !ledgerId ||
                typeof id !== "string" ||
                !id ||
                !["create", "save", "recalculate", "manual", "transition"].includes(action) ||
                (action === "create"
                    ? data.id !== id
                    : !Number.isInteger(data.expected_version) ||
                      (data.expected_version as number) < 1 ||
                      (data.expected_version as number) > 2147483646)
            )
                throw new ApiError("server", null, "invalid_reminder_intent");
            this.uncertain = false;
            this.publish({
                ...idle(),
                plan: Object.freeze({
                    ownerId: this.owner!,
                    ledgerId,
                    id,
                    action,
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
                throw new ApiError("server", null, "reminder_retry_unavailable");
            return this.send(session);
        } catch (error) {
            return Promise.reject(error);
        }
    }
    private send(session: Session): Promise<void> {
        const generation = this.generation,
            plan = this.state.plan!;
        this.publish({ ...this.state, status: "submitting", error: null });
        const flight = Promise.resolve().then(async () => {
            try {
                if (generation !== this.generation) return;
                if (this.owner !== String(session.user.id)) {
                    this.publish({ ...this.state, status: "auth-required" });
                    return;
                }
                const value = await this.io(session, plan);
                if (generation !== this.generation) return;
                if (!scopedReminder(value, plan) || !matchesReminder(value, plan))
                    throw new ApiError("server", 200, "invalid_response");
                this.uncertain = false;
                this.publish({
                    ...this.state,
                    status: "confirmed",
                    record: capture(value),
                });
            } catch (caught) {
                if (generation !== this.generation) return;
                let error = caught instanceof ApiError ? caught : new ApiError("network");
                let status: ReminderStatus = "unknown";
                let record: Reminder | null = null;
                if (error.kind === "unauthorized") status = "auth-required";
                else if (
                    (error.status === 409 &&
                        ((plan.action === "create" && error.code === "duplicate_record") ||
                            (plan.action !== "create" && error.code === "version_conflict"))) ||
                    this.uncertain
                ) {
                    try {
                        const current = await this.read(plan);
                        if (generation !== this.generation) return;
                        if (!scopedReminder(current, plan))
                            throw new ApiError("server", 200, "invalid_response");
                        if (plan.action === "create")
                            status = matchesReminder(current, plan) ? "confirmed" : "conflict";
                        else if (
                            !this.uncertain ||
                            current.version > JSON.parse(plan.bodyJson).expected_version
                        )
                            status = "conflict";
                        if (status !== "unknown") record = capture(current);
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
        return flight;
    }
}
