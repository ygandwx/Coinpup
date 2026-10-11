import type {
    BusinessAllocationCreate,
    BusinessAllocationReceipt,
    BusinessPaymentCreate,
    BusinessPaymentReceipt,
} from "./business-settlement-api";
import { validDraftDate } from "./draft-fields";
import { parseAmount } from "./money";

export type SettlementInput =
    | { kind: "payment"; body: BusinessPaymentCreate }
    | { kind: "allocation"; body: BusinessAllocationCreate };
export type SettlementReceipt = BusinessPaymentReceipt | BusinessAllocationReceipt;
export type FrozenSettlement = Readonly<{
    ownerId: string;
    ledgerId: string;
    kind: SettlementInput["kind"];
    key: string;
    bodyJson: string;
    reviewedJson: string;
}>;
const record = (value: unknown): value is Record<string, unknown> =>
    typeof value === "object" && value !== null && !Array.isArray(value);
const uuid = (value: unknown): value is string =>
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u.test(value);
const date = (value: unknown) => typeof value === "string" && validDraftDate(value);
const asset = (value: unknown) =>
    typeof value === "string" && value.length > 0 && value.length <= 200 && !value.includes("\0");
const positive = (value: unknown): boolean => {
    try {
        return typeof value === "string" && parseAmount(value, 18) > 0n;
    } catch {
        return false;
    }
};
const keys = (value: Record<string, unknown>, allowed: string[]) =>
    Object.keys(value).every((key) => allowed.includes(key));

function groupInput(value: unknown, standalone: boolean): boolean {
    if (!record(value) || !uuid(value.id) || !date(value.allocation_date)) return false;
    if (!keys(value, ["id", "allocation_date", "lines", ...(standalone ? ["payment_id"] : [])]))
        return false;
    if (standalone && !uuid(value.payment_id)) return false;
    if (!Array.isArray(value.lines) || value.lines.length < 1 || value.lines.length > 200)
        return false;
    const sources = new Set<string>();
    return value.lines.every((line) => {
        if (
            !record(line) ||
            !keys(line, ["document_id", "document_line_id", "payment_amount", "document_amount"]) ||
            !uuid(line.document_id) ||
            !uuid(line.document_line_id) ||
            !positive(line.payment_amount) ||
            !positive(line.document_amount) ||
            sources.has(line.document_line_id)
        )
            return false;
        sources.add(line.document_line_id);
        return true;
    });
}

export function validSettlementInput(ledgerId: string, input: SettlementInput): boolean {
    if (!uuid(ledgerId) || !record(input) || !record(input.body)) return false;
    if (input.kind === "allocation") return groupInput(input.body, true);
    if (input.kind !== "payment") return false;
    const body = input.body;
    if (
        !keys(body, [
            "id",
            "direction",
            "party_id",
            "account_id",
            "asset_id",
            "amount",
            "transaction_date",
            "description",
            "fees",
            "initial_allocation",
        ])
    )
        return false;
    if (
        !uuid(body.id) ||
        !uuid(body.party_id) ||
        !uuid(body.account_id) ||
        !asset(body.asset_id) ||
        !positive(body.amount) ||
        !date(body.transaction_date) ||
        !["receipt", "payout"].includes(body.direction as string)
    )
        return false;
    if (
        body.description !== undefined &&
        (typeof body.description !== "string" ||
            body.description.length > 2000 ||
            body.description.includes("\0"))
    )
        return false;
    const fees = body.fees === undefined ? [] : body.fees;
    if (
        !Array.isArray(fees) ||
        fees.length > 20 ||
        !fees.every(
            (fee) =>
                record(fee) &&
                keys(fee, ["account_id", "asset_id", "amount", "category_id"]) &&
                uuid(fee.account_id) &&
                uuid(fee.category_id) &&
                asset(fee.asset_id) &&
                positive(fee.amount),
        )
    )
        return false;
    return body.initial_allocation == null || groupInput(body.initial_allocation, false);
}

function groupReceipt(
    value: unknown,
    expected: Record<string, unknown>,
    ledger: string,
    payment: string,
): boolean {
    if (
        !record(value) ||
        !keys(value, ["kind", "id", "ledger_id", "payment_id", "postings"]) ||
        value.kind !== "business_allocation" ||
        value.id !== expected.id ||
        value.ledger_id !== ledger ||
        value.payment_id !== payment ||
        !Array.isArray(value.postings) ||
        !Array.isArray(expected.lines) ||
        value.postings.length !== expected.lines.length ||
        value.postings.length < 1 ||
        value.postings.length > 200
    )
        return false;
    const operations = new Set<string>(),
        journals = new Set<string>();
    return value.postings.every((line, index) => {
        const source = (expected.lines as unknown[])[index];
        if (
            !record(line) ||
            !record(source) ||
            !keys(line, [
                "line_no",
                "document_id",
                "document_line_id",
                "operation_id",
                "journal_id",
            ]) ||
            line.line_no !== index + 1 ||
            line.document_id !== source.document_id ||
            line.document_line_id !== source.document_line_id ||
            !uuid(line.operation_id) ||
            !uuid(line.journal_id) ||
            operations.has(line.operation_id) ||
            journals.has(line.journal_id)
        )
            return false;
        operations.add(line.operation_id);
        journals.add(line.journal_id);
        return true;
    });
}

export function validSettlementReceipt(
    value: unknown,
    plan: FrozenSettlement,
): value is SettlementReceipt {
    if (!record(value)) return false;
    const body = JSON.parse(plan.bodyJson) as Record<string, unknown>;
    if (plan.kind === "allocation")
        return groupReceipt(value, body, plan.ledgerId, body.payment_id as string);
    if (
        !keys(value, [
            "kind",
            "id",
            "ledger_id",
            "operation_id",
            "journal_id",
            "initial_allocation",
        ]) ||
        value.kind !== "business_payment" ||
        value.id !== body.id ||
        value.ledger_id !== plan.ledgerId ||
        !uuid(value.operation_id) ||
        !uuid(value.journal_id)
    )
        return false;
    if (body.initial_allocation == null) return value.initial_allocation === null;
    if (
        !record(body.initial_allocation) ||
        !groupReceipt(
            value.initial_allocation,
            body.initial_allocation,
            plan.ledgerId,
            body.id as string,
        )
    )
        return false;
    return (value.initial_allocation as BusinessAllocationReceipt).postings.every(
        (line) => line.operation_id !== value.operation_id && line.journal_id !== value.journal_id,
    );
}

export type DeepReadonly<T> = T extends (infer U)[]
    ? readonly DeepReadonly<U>[]
    : T extends object
      ? { readonly [K in keyof T]: DeepReadonly<T[K]> }
      : T;
export function freezeSettlementReceipt(value: SettlementReceipt): DeepReadonly<SettlementReceipt> {
    const copy = JSON.parse(JSON.stringify(value)) as SettlementReceipt;
    const freeze = (node: unknown): void => {
        if (node !== null && typeof node === "object") {
            Object.values(node).forEach(freeze);
            Object.freeze(node);
        }
    };
    freeze(copy);
    return copy;
}
