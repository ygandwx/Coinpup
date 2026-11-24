import { ApiError } from "./api";
import type { Session } from "./api";
import { issueInvoice, getInvoiceIssuance } from "./invoice-api";
import { freezeInvoice } from "./invoice-web-intent";
import type { FrozenInvoice, InvoiceReview } from "./invoice-web-intent";
import { validInvoiceState } from "./invoice-issuance-receipt";
import type { InvoiceCommand, InvoiceState, InvoiceComplete } from "./invoice-issuance-receipt";
import type { PendingStatus } from "./pending-command";
import type { DeepReadonly } from "./settlement-intent";

type Snapshot = Readonly<{
    status: PendingStatus;
    plan: FrozenInvoice | null;
    receipt: DeepReadonly<InvoiceComplete> | null;
    observed: DeepReadonly<InvoiceState> | null;
    checking: boolean;
    error: ApiError | null;
}>;
type Send = (session: Session, plan: FrozenInvoice) => Promise<unknown>;
type Read = (plan: FrozenInvoice) => Promise<unknown>;
const empty = (): Snapshot =>
    Object.freeze({
        status: "idle",
        plan: null,
        receipt: null,
        observed: null,
        checking: false,
        error: null,
    });
const terminal = (s: PendingStatus) => ["idle", "confirmed", "rejected"].includes(s);
const bad = () => new ApiError("server", null, "pending_invoice_unresolved");
// A client error can arrive after the number commits. Only these POST errors
// are emitted exclusively before the reservation succeeds.
const preReservationRejections = new Map([
    [
        409,
        new Set([
            "entity_archived",
            "version_conflict",
            "invoice_not_issuable",
            "invoice_profile_archived",
            "invoice_logo_unavailable",
            "invoice_predecessor_conflict",
            "invoice_sequence_exhausted",
            "invoice_number_conflict",
        ]),
    ],
    [
        422,
        new Set([
            "invalid_request",
            "invalid_idempotency_key",
            "invoice_logo_size_limit",
            "invoice_logo_invalid",
        ]),
    ],
]);
function lockResult<T extends InvoiceState>(result: T): DeepReadonly<T> {
    const copy = structuredClone(result);
    Object.freeze(copy.numbering);
    copy.artifacts.forEach(Object.freeze);
    Object.freeze(copy.artifacts);
    return Object.freeze(copy) as DeepReadonly<T>;
}
export class PendingInvoiceController {
    private state = empty();
    private visible = this.state;
    private generation = 0;
    private flight: Promise<void> | null = null;
    private owner: string | null = null;
    private uncertain = false;
    private listeners = new Set<() => void>();
    private send: Send;
    private read: Read;
    constructor(
        send: Send = (s, p) =>
            issueInvoice(s.csrf_token, p.ledgerId, JSON.parse(p.bodyJson), p.key),
        read: Read = (p) => getInvoiceIssuance(p.ledgerId, JSON.parse(p.bodyJson).id),
    ) {
        this.send = send;
        this.read = read;
    }
    getSnapshot = () => this.visible;
    subscribe = (listener: () => void) => {
        this.listeners.add(listener);
        return () => {
            this.listeners.delete(listener);
        };
    };
    private publish(next: Snapshot) {
        this.state = Object.freeze(next);
        this.visible =
            next.status === "auth-required" || (this.owner === null && next.plan !== null)
                ? Object.freeze({ ...empty(), status: "auth-required" })
                : this.state;
        for (const listener of [...this.listeners]) listener();
    }
    setOwner(owner: string | null, explicitLogout = false) {
        if (owner === this.owner && !explicitLogout) return;
        this.generation++;
        this.flight = null;
        this.owner = owner;
        const previous = this.state;
        if (
            explicitLogout ||
            (owner !== null && previous.plan && previous.plan.ownerId !== owner)
        ) {
            this.uncertain = false;
            this.publish(empty());
            return;
        }
        if (
            previous.plan &&
            !terminal(previous.status) &&
            previous.status !== "idempotency-conflict"
        ) {
            this.uncertain = true;
            this.publish({
                ...previous,
                status: owner === null ? "auth-required" : "unknown",
                checking: false,
                error: null,
            });
        } else this.publish({ ...previous, checking: false });
    }
    dismiss = () => {
        if (!terminal(this.state.status) || this.flight) return false;
        this.generation++;
        this.uncertain = false;
        this.publish(empty());
        return true;
    };
    private authorize(session: Session) {
        if (
            !this.owner ||
            this.owner !== String(session.user.id) ||
            (this.state.plan && this.state.plan.ownerId !== this.owner)
        )
            throw new ApiError("unauthorized", 401, "invoice_owner_changed");
    }
    async start(session: Session, ledgerId: string, review: InvoiceReview) {
        this.authorize(session);
        if (this.flight || !terminal(this.state.status)) throw bad();
        const plan = freezeInvoice(this.owner!, ledgerId, review);
        this.uncertain = false;
        this.state = { ...empty(), plan };
        await this.run(session, false);
    }
    async retry(session: Session) {
        this.authorize(session);
        if (this.flight || this.state.status !== "unknown" || !this.state.plan) throw bad();
        await this.run(session, false);
    }
    async inspect(session: Session) {
        this.authorize(session);
        if (
            this.flight ||
            !this.state.plan ||
            !["unknown", "idempotency-conflict"].includes(this.state.status)
        )
            throw bad();
        await this.run(session, true);
    }
    private run(session: Session, readOnly: boolean) {
        const epoch = this.generation,
            p = this.state.plan!,
            status = this.state.status;
        const flight = Promise.resolve().then(async () => {
            try {
                if (epoch !== this.generation) return;
                const value = await (readOnly ? this.read(p) : this.send(session, p));
                if (epoch !== this.generation) return;
                const command: InvoiceCommand = JSON.parse(p.bodyJson),
                    review: InvoiceReview = JSON.parse(p.reviewedJson);
                const prev = this.state.observed;
                if (
                    !validInvoiceState(value, p.ledgerId, command, review.profile.numbering) ||
                    (prev &&
                        (value.sequence !== prev.sequence ||
                            value.snapshot_sha256 !== prev.snapshot_sha256 ||
                            value.number !== prev.number ||
                            (prev.state === "complete" &&
                                (value.state !== "complete" ||
                                    !prev.artifacts.every((a, i) => {
                                        const b = value.artifacts[i];
                                        return (
                                            b &&
                                            a.language === b.language &&
                                            a.file_id === b.file_id &&
                                            a.sha256 === b.sha256 &&
                                            a.byte_size === b.byte_size &&
                                            a.media_type === b.media_type
                                        );
                                    })))))
                )
                    throw new ApiError("server", 200, "invalid_response");
                // GET can show what exists but never has the command-key/JSON binding of a POST replay.
                if (readOnly) {
                    this.publish({
                        ...this.state,
                        status,
                        checking: false,
                        observed: lockResult(value),
                        error: null,
                    });
                    return;
                }
                if (value.state !== "complete")
                    throw new ApiError("server", 201, "invalid_response");
                this.uncertain = false;
                this.publish({
                    ...this.state,
                    status: "confirmed",
                    checking: false,
                    receipt: lockResult(value),
                    error: null,
                });
            } catch (caught) {
                if (epoch !== this.generation) return;
                const error = caught instanceof ApiError ? caught : new ApiError("network");
                let next: PendingStatus;
                if (error.kind === "unauthorized") {
                    next = status === "idempotency-conflict" ? status : "auth-required";
                    this.uncertain = true;
                    this.owner = null;
                } else if (readOnly) next = status;
                else if (error.code === "idempotency_conflict") next = "idempotency-conflict";
                else if (
                    !this.uncertain &&
                    error.status !== null &&
                    error.code !== null &&
                    preReservationRejections.get(error.status)?.has(error.code)
                )
                    next = "rejected";
                else {
                    this.uncertain = true;
                    next = "unknown";
                }
                this.publish({ ...this.state, status: next, checking: false, error });
            } finally {
                if (epoch === this.generation) this.flight = null;
            }
        });
        this.flight = flight;
        this.publish({
            ...this.state,
            status: readOnly ? status : "submitting",
            checking: readOnly,
            error: null,
        });
        return flight;
    }
}
