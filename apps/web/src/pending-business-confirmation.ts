import { ApiError } from "./api";
import type { Session } from "./api";
import { getBusinessDraft } from "./business-draft-api";
import type { BusinessDraft } from "./business-draft-api";
import { confirmBusinessDocument, getBusinessConfirmation } from "./business-confirmation-api";
import type {
    BusinessConfirmationCreate,
    BusinessConfirmationReceipt,
} from "./business-confirmation-api";
import type { Asset } from "./ledger-api";
import { parseAmount } from "./money";
import type { PendingStatus } from "./pending-command";

type Member = Readonly<{ line_id: string; line_no: number }>;
type Receipt = Readonly<Omit<BusinessConfirmationReceipt, "postings">> & {
    readonly postings: readonly Readonly<BusinessConfirmationReceipt["postings"][number]>[];
};
export type FrozenBusinessConfirmation = Readonly<{
    ownerId: string;
    ledgerId: string;
    documentId: string;
    key: string;
    bodyJson: string;
    reviewedJson: string;
    members: readonly Member[];
}>;
export type BusinessConfirmationSnapshot = Readonly<{
    status: PendingStatus;
    plan: FrozenBusinessConfirmation | null;
    receipt: Receipt | null;
    error: ApiError | null;
}>;
type Transport = (session: Session, plan: FrozenBusinessConfirmation) => Promise<unknown>;
type ReadVersion = (ledgerId: string, documentId: string) => Promise<unknown>;
const transport: Transport = (session, plan) =>
    confirmBusinessDocument(
        session.csrf_token,
        plan.ledgerId,
        plan.documentId,
        JSON.parse(plan.bodyJson),
        plan.key,
    );
const readVersion: ReadVersion = async (ledgerId, documentId) => {
    try {
        return await getBusinessDraft(ledgerId, documentId);
    } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 404) throw error;
        return (await getBusinessConfirmation(ledgerId, documentId)).document;
    }
};
const idle = (): BusinessConfirmationSnapshot =>
    Object.freeze({ status: "idle", plan: null, receipt: null, error: null });
const uuid = (value: unknown): value is string =>
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u.test(value);

function sourceMembers(document: BusinessDraft, asset: Asset): Member[] | null {
    try {
        if (
            !uuid(document.id) ||
            !uuid(document.ledger_id) ||
            document.state !== "draft" ||
            document.archived ||
            !Number.isInteger(document.version) ||
            document.version < 1 ||
            document.version > 2147483646 ||
            !["invoice", "bill"].includes(document.document_kind) ||
            asset.asset_id !== document.asset_id ||
            !asset.enabled ||
            !Array.isArray(document.lines) ||
            document.lines.length < 1 ||
            document.lines.length > 200 ||
            document.line_count !== document.lines.length
        )
            return null;
        const ids = new Set<string>(),
            positions = new Set<number>(),
            members: Member[] = [];
        let total = 0n,
            net = 0n,
            tax = 0n;
        for (const line of document.lines) {
            if (
                !uuid(line.id) ||
                ids.has(line.id) ||
                !Number.isInteger(line.line_no) ||
                line.line_no < 1 ||
                line.line_no > 2147483647 ||
                positions.has(line.line_no) ||
                line.document_id !== document.id ||
                line.ledger_id !== document.ledger_id ||
                line.asset_id !== document.asset_id ||
                line.archived
            )
                return null;
            ids.add(line.id);
            positions.add(line.line_no);
            const n = parseAmount(line.net_amount, asset.scale),
                t = parseAmount(line.tax_amount, asset.scale);
            const gross = parseAmount(line.total_amount, asset.scale);
            if (n < 0n || t < 0n || gross !== n + t) return null;
            net += n;
            tax += t;
            total += gross;
            if (gross > 0n)
                members.push(Object.freeze({ line_id: line.id, line_no: line.line_no }));
        }
        if (
            total <= 0n ||
            total !== parseAmount(document.total_amount, asset.scale) ||
            net !== parseAmount(document.net_amount, asset.scale) ||
            tax !== parseAmount(document.tax_amount, asset.scale)
        )
            return null;
        return members.sort((a, b) => a.line_no - b.line_no);
    } catch {
        return null;
    }
}

function valid(
    value: unknown,
    plan: FrozenBusinessConfirmation,
): value is BusinessConfirmationReceipt {
    if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
    const receipt = value as Record<string, unknown>;
    const body = JSON.parse(plan.bodyJson) as BusinessConfirmationCreate;
    if (
        receipt.kind !== "business_confirmation" ||
        receipt.id !== body.id ||
        receipt.ledger_id !== plan.ledgerId ||
        receipt.document_id !== plan.documentId ||
        receipt.source_version !== body.expected_version ||
        receipt.document_version !== body.expected_version + 1 ||
        !Array.isArray(receipt.postings) ||
        receipt.postings.length !== plan.members.length
    )
        return false;
    const operations = new Set<string>(),
        journals = new Set<string>();
    return receipt.postings.every((row: unknown, index: number) => {
        if (row === null || typeof row !== "object" || Array.isArray(row)) return false;
        const member = row as Record<string, unknown>,
            expected = plan.members[index];
        if (
            member.line_id !== expected.line_id ||
            member.line_no !== expected.line_no ||
            !uuid(member.operation_id) ||
            !uuid(member.journal_id) ||
            operations.has(member.operation_id) ||
            journals.has(member.journal_id)
        )
            return false;
        operations.add(member.operation_id);
        journals.add(member.journal_id);
        return true;
    });
}

/** One reviewed version in memory; no GET can substitute for the original command receipt. */
export class PendingBusinessConfirmationController {
    private owner: string | null = null;
    private state = idle();
    private visible = this.state;
    private generation = 0;
    private uncertain = false;
    private flight: Promise<void> | null = null;
    private listeners = new Set<() => void>();
    private io: Transport;
    private read: ReadVersion;
    constructor(io: Transport = transport, read: ReadVersion = readVersion) {
        this.io = io;
        this.read = read;
    }
    getSnapshot = (): BusinessConfirmationSnapshot => this.visible;
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
            throw new ApiError("unauthorized", 401, "confirmation_owner_changed");
    }
    start(session: Session, document: BusinessDraft, asset: Asset): Promise<void> {
        try {
            this.authorized(session);
            if (this.flight || !["idle", "confirmed", "rejected"].includes(this.state.status))
                throw new ApiError("server", null, "pending_confirmation_unresolved");
            const members = sourceMembers(document, asset);
            if (!members) throw new ApiError("server", null, "invalid_confirmation_intent");
            const body: BusinessConfirmationCreate = {
                id: crypto.randomUUID(),
                expected_version: document.version,
            };
            this.uncertain = false;
            this.publish({
                ...idle(),
                plan: Object.freeze({
                    ownerId: this.owner!,
                    ledgerId: document.ledger_id,
                    documentId: document.id,
                    key: crypto.randomUUID(),
                    bodyJson: JSON.stringify(body),
                    reviewedJson: JSON.stringify(document),
                    members: Object.freeze(members),
                }),
            });
            return this.send(session);
        } catch (error) {
            return Promise.reject(
                error instanceof ApiError
                    ? error
                    : new ApiError("server", null, "invalid_confirmation_intent"),
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
                throw new ApiError("server", null, "confirmation_retry_unavailable");
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
                const result = await this.io(session, plan);
                if (generation !== this.generation) return;
                if (!valid(result, plan)) throw new ApiError("server", 201, "invalid_response");
                this.uncertain = false;
                this.publish({
                    ...this.state,
                    status: "confirmed",
                    receipt: Object.freeze({
                        ...result,
                        postings: Object.freeze(
                            result.postings.map((row) => Object.freeze({ ...row })),
                        ),
                    }),
                });
            } catch (caught) {
                if (generation !== this.generation) return;
                const error = caught instanceof ApiError ? caught : new ApiError("network");
                let status: PendingStatus;
                if (error.kind === "unauthorized") status = "auth-required";
                else if (error.code === "idempotency_conflict") status = "idempotency-conflict";
                else if (
                    this.uncertain &&
                    error.status === 409 &&
                    ["version_conflict", "document_confirmed"].includes(error.code ?? "")
                ) {
                    status = "unknown";
                    try {
                        const current = await this.read(plan.ledgerId, plan.documentId);
                        if (generation !== this.generation) return;
                        const observed = current as Record<string, unknown> | null;
                        if (
                            observed?.id === plan.documentId &&
                            observed.ledger_id === plan.ledgerId &&
                            (observed.state === "draft" || observed.state === "confirmed") &&
                            Number.isInteger(observed.version) &&
                            (observed.version as number) >
                                JSON.parse(plan.bodyJson).expected_version &&
                            (observed.version as number) <= 2147483647
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
