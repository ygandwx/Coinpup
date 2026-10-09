import { ApiError } from "./api";
import type { Session } from "./api";
import { getMasterData, saveMasterData } from "./business-api";
import type { MasterBody, MasterKind, MasterRecord } from "./business-api";

export type MasterStatus =
    "idle" | "submitting" | "unknown" | "auth-required" | "confirmed" | "conflict" | "rejected";
export type FrozenMaster = Readonly<{
    ownerId: string;
    ledgerId: string;
    kind: MasterKind;
    id: string;
    method: "POST" | "PATCH";
    bodyJson: string;
}>;
export type MasterSnapshot = Readonly<{
    status: MasterStatus;
    plan: FrozenMaster | null;
    record: MasterRecord | null;
    error: ApiError | null;
}>;
type Transport = (session: Session, plan: FrozenMaster) => Promise<unknown>;
type Reader = (plan: FrozenMaster) => Promise<unknown>;
const terminal = ["idle", "confirmed", "conflict", "rejected"];
const idle = (): MasterSnapshot =>
    Object.freeze({ status: "idle", plan: null, record: null, error: null });

/** Match Python str.strip for names without changing other submitted text. */
export function masterName(value: string): string {
    const blank = (char: string) =>
        /^\p{White_Space}$/u.test(char) || (char >= "\u001c" && char <= "\u001f");
    let start = 0,
        end = value.length;
    while (start < end && blank(value[start])) start += 1;
    while (end > start && blank(value[end - 1])) end -= 1;
    return value.slice(start, end);
}
function scoped(value: unknown, plan: FrozenMaster): value is MasterRecord {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const row = value as Record<string, unknown>;
    const text = (field: string) => row[field] === null || typeof row[field] === "string";
    return (
        row.id === plan.id &&
        row.ledger_id === plan.ledgerId &&
        Number.isInteger(row.version) &&
        (row.version as number) > 0 &&
        (row.version as number) <= 2147483647 &&
        typeof row.archived === "boolean" &&
        ["created_at", "updated_at"].every(
            (key) =>
                typeof row[key] === "string" && Number.isFinite(Date.parse(row[key] as string)),
        ) &&
        text("notes") &&
        (plan.kind === "projects"
            ? typeof row.name === "string"
            : text("name") &&
              (row.name === null
                  ? row.role === null
                  : ["customer", "supplier", "both"].includes(row.role as string)) &&
              ["legal_name", "email", "phone", "address", "tax_identifier"].every(text))
    );
}
function matches(value: MasterRecord, plan: FrozenMaster): boolean {
    const body = JSON.parse(plan.bodyJson) as Record<string, unknown>;
    const row = value as unknown as Record<string, unknown>;
    const expected =
        plan.method === "POST"
            ? {
                  notes: null,
                  archived: false,
                  ...(plan.kind === "parties"
                      ? {
                            legal_name: null,
                            email: null,
                            phone: null,
                            address: null,
                            tax_identifier: null,
                        }
                      : {}),
                  ...body,
              }
            : body;
    return (
        value.version === (plan.method === "POST" ? 1 : (body.expected_version as number) + 1) &&
        Object.entries(expected).every(
            ([key, item]) =>
                key === "expected_version" ||
                row[key] ===
                    (["name", "legal_name"].includes(key) && typeof item === "string"
                        ? masterName(item)
                        : item),
        )
    );
}

export class PendingMasterDataController {
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
            saveMasterData(
                session.csrf_token,
                plan.ledgerId,
                plan.kind,
                plan.id,
                plan.method,
                JSON.parse(plan.bodyJson),
            ),
        read: Reader = (plan) => getMasterData(plan.ledgerId, plan.kind, plan.id),
    ) {
        this.io = io;
        this.read = read;
    }
    getSnapshot = (): MasterSnapshot => this.visible;
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
            throw new ApiError("unauthorized", 401, "master_owner_changed");
    }
    start(
        session: Session,
        ledgerId: string,
        kind: MasterKind,
        id: string,
        method: "POST" | "PATCH",
        body: MasterBody,
    ): Promise<void> {
        try {
            this.authorized(session);
            if (this.flight || !terminal.includes(this.state.status))
                throw new ApiError("server", null, "pending_master_unresolved");
            const data = body as unknown as Record<string, unknown>;
            if (
                !ledgerId ||
                !id ||
                !["parties", "projects"].includes(kind) ||
                !["POST", "PATCH"].includes(method) ||
                (method === "POST"
                    ? data.id !== id
                    : !Number.isInteger(data.expected_version) ||
                      (data.expected_version as number) < 1 ||
                      (data.expected_version as number) > 2147483646)
            )
                throw new ApiError("server", null, "invalid_master_intent");
            this.uncertain = false;
            this.publish({
                ...idle(),
                plan: Object.freeze({
                    ownerId: this.owner!,
                    ledgerId,
                    kind,
                    id,
                    method,
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
                throw new ApiError("server", null, "master_retry_unavailable");
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
                if (!scoped(value, plan) || !matches(value, plan))
                    throw new ApiError("server", 200, "invalid_response");
                this.uncertain = false;
                this.publish({
                    ...this.state,
                    status: "confirmed",
                    record: Object.freeze({ ...value }),
                });
            } catch (caught) {
                if (generation !== this.generation) return;
                let error = caught instanceof ApiError ? caught : new ApiError("network");
                let status: MasterStatus = "unknown";
                let record: MasterRecord | null = null;
                if (error.kind === "unauthorized") status = "auth-required";
                else if (
                    error.status === 409 &&
                    ((plan.method === "POST" && error.code === "duplicate_record") ||
                        (plan.method === "PATCH" && error.code === "version_conflict"))
                ) {
                    try {
                        const current = await this.read(plan);
                        if (generation !== this.generation) return;
                        if (!scoped(current, plan))
                            throw new ApiError("server", 200, "invalid_response");
                        if (plan.method === "POST")
                            status = matches(current, plan) ? "confirmed" : "conflict";
                        else if (
                            !this.uncertain ||
                            current.version > JSON.parse(plan.bodyJson).expected_version
                        )
                            status = "conflict";
                        if (status !== "unknown") record = Object.freeze({ ...current });
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
