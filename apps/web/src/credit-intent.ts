import type { CreditCapacity, CreditCreate, CreditReceipt } from "./credit-api";
import type { BusinessConfirmationReceipt } from "./business-confirmation-api";
import type { Asset } from "./ledger-api";
import type { DeepReadonly } from "./settlement-intent";
import { validDraftDate } from "./draft-fields";
import { parseAmount } from "./money";

export type CreditReview = {
    capacity: CreditCapacity;
    confirmation: BusinessConfirmationReceipt;
    asset: Asset;
};
export type FrozenCredit = Readonly<{
    ownerId: string;
    ledgerId: string;
    key: string;
    bodyJson: string;
    reviewedJson: string;
}>;
export const record = (v: unknown): v is Record<string, unknown> =>
    v !== null && typeof v === "object" && !Array.isArray(v);
export const uuid = (v: unknown): v is string =>
    typeof v === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u.test(v);
export const keys = (v: Record<string, unknown>, names: string[]) =>
    Object.keys(v).every((key) => names.includes(key));
export const date = (v: unknown): v is string => typeof v === "string" && validDraftDate(v);
export const quantity = (v: unknown, scale: number): bigint => {
    if (typeof v !== "string" || !Number.isInteger(scale) || scale < 0 || scale > 18)
        throw new Error("Invalid quantity");
    const amount = parseAmount(v, scale);
    if (amount < 0n || amount >= 10n ** BigInt(20 + scale)) throw new Error("Invalid quantity");
    return amount;
};

export function validCreditInput(value: unknown): value is CreditCreate {
    if (
        !record(value) ||
        !keys(value, ["id", "document_id", "expected_version", "credit_date", "reason", "lines"]) ||
        !uuid(value.id) ||
        !uuid(value.document_id) ||
        !Number.isSafeInteger(value.expected_version) ||
        (value.expected_version as number) < 1 ||
        (value.expected_version as number) > 2147483647 ||
        !date(value.credit_date) ||
        typeof value.reason !== "string" ||
        !value.reason.trim() ||
        value.reason.includes("\0") ||
        value.reason.length > 2000 ||
        !Array.isArray(value.lines) ||
        value.lines.length < 1 ||
        value.lines.length > 200
    )
        return false;
    const sources = new Set<string>();
    try {
        return value.lines.every((line) => {
            if (
                !record(line) ||
                !keys(line, ["source_line_id", "net_amount", "tax_amount", "recognition_date"]) ||
                !uuid(line.source_line_id) ||
                sources.has(line.source_line_id) ||
                !date(line.recognition_date)
            )
                return false;
            sources.add(line.source_line_id);
            const total = quantity(line.net_amount, 18) + quantity(line.tax_amount, 18);
            return total > 0n && total < 10n ** 38n;
        });
    } catch {
        return false;
    }
}

export function validCreditReview(ledgerId: string, body: unknown, value: unknown): boolean {
    if (
        !uuid(ledgerId) ||
        !validCreditInput(body) ||
        !record(value) ||
        !record(value.capacity) ||
        !record(value.confirmation) ||
        !record(value.asset)
    )
        return false;
    const { capacity, confirmation, asset } = value as unknown as CreditReview;
    if (
        capacity.id !== body.document_id ||
        capacity.ledger_id !== ledgerId ||
        capacity.expected_version !== body.expected_version ||
        !uuid(capacity.party_id) ||
        !["invoice", "bill"].includes(capacity.document_kind) ||
        !date(capacity.issue_date) ||
        body.credit_date < capacity.issue_date ||
        asset.asset_id !== capacity.asset_id ||
        typeof asset.asset_id !== "string" ||
        !asset.asset_id ||
        asset.asset_id.length > 200 ||
        asset.asset_id.includes("\0") ||
        asset.enabled !== true ||
        !Array.isArray(capacity.lines) ||
        !capacity.lines.every(record) ||
        capacity.lines.length > 200 ||
        confirmation.kind !== "business_confirmation" ||
        !uuid(confirmation.id) ||
        confirmation.ledger_id !== ledgerId ||
        confirmation.document_id !== capacity.id ||
        confirmation.document_version !== body.expected_version ||
        confirmation.source_version + 1 !== confirmation.document_version ||
        !Array.isArray(confirmation.postings) ||
        !confirmation.postings.every(record) ||
        confirmation.postings.length !== capacity.lines.length ||
        new Set(capacity.lines.map((line) => line.source_line_id)).size !== capacity.lines.length ||
        new Set(confirmation.postings.map((line) => line.line_id)).size !==
            confirmation.postings.length ||
        new Set(confirmation.postings.map((line) => line.operation_id)).size !==
            confirmation.postings.length
    )
        return false;
    try {
        return body.lines.every((line) => {
            const source = capacity.lines.find((row) => row.source_line_id === line.source_line_id);
            const original = confirmation.postings.find(
                (row) => row.line_id === line.source_line_id,
            );
            if (
                !source ||
                !original ||
                !uuid(original.operation_id) ||
                !uuid(original.journal_id) ||
                !uuid(source.category_id) ||
                (source.project_id !== null && !uuid(source.project_id)) ||
                typeof source.description !== "string" ||
                !date(source.recognition_date)
            )
                return false;
            const net = quantity(line.net_amount, asset.scale),
                tax = quantity(line.tax_amount, asset.scale);
            const originalNet = quantity(source.net_amount, asset.scale);
            const originalTax = quantity(source.tax_amount, asset.scale);
            const total = quantity(source.total_amount, asset.scale);
            const creditedNet = quantity(source.credited_net_amount, asset.scale);
            const creditedTax = quantity(source.credited_tax_amount, asset.scale);
            const applied = quantity(source.applied_amount, asset.scale);
            return (
                originalNet + originalTax === total &&
                originalNet - creditedNet === quantity(source.remaining_net_amount, asset.scale) &&
                originalTax - creditedTax === quantity(source.remaining_tax_amount, asset.scale) &&
                total - applied - creditedNet - creditedTax ===
                    quantity(source.available_gross_amount, asset.scale) &&
                net <= originalNet - creditedNet &&
                tax <= originalTax - creditedTax &&
                net + tax <= total - applied - creditedNet - creditedTax
            );
        });
    } catch {
        return false;
    }
}

export function validCreditReceipt(value: unknown, plan: FrozenCredit): value is CreditReceipt {
    try {
        const body = JSON.parse(plan.bodyJson) as CreditCreate;
        const review = JSON.parse(plan.reviewedJson) as CreditReview;
        if (
            !validCreditReview(plan.ledgerId, body, review) ||
            !record(value) ||
            !keys(value, [
                "kind",
                "id",
                "ledger_id",
                "document_id",
                "source_version",
                "asset_id",
                "credit_date",
                "postings",
            ]) ||
            value.kind !== "business_credit" ||
            value.id !== body.id ||
            value.ledger_id !== plan.ledgerId ||
            value.document_id !== body.document_id ||
            value.source_version !== body.expected_version ||
            value.asset_id !== review.asset.asset_id ||
            value.credit_date !== body.credit_date ||
            !Array.isArray(value.postings) ||
            value.postings.length !== body.lines.length
        )
            return false;
        const operations = new Set<string>(),
            journals = new Set<string>();
        const originals = new Set(review.confirmation.postings.map((row) => row.operation_id));
        return value.postings.every((row, index) => {
            const requested = body.lines[index];
            const original = review.confirmation.postings.find(
                (item) => item.line_id === requested.source_line_id,
            )!;
            if (
                !record(row) ||
                !keys(row, [
                    "line_no",
                    "source_line_id",
                    "source_operation_id",
                    "net_amount",
                    "tax_amount",
                    "total_amount",
                    "recognition_date",
                    "operation_id",
                    "journal_id",
                ]) ||
                row.line_no !== index + 1 ||
                row.source_line_id !== requested.source_line_id ||
                row.source_operation_id !== original.operation_id ||
                row.recognition_date !== requested.recognition_date ||
                !uuid(row.operation_id) ||
                !uuid(row.journal_id) ||
                operations.has(row.operation_id) ||
                journals.has(row.journal_id) ||
                originals.has(row.operation_id) ||
                row.journal_id === original.journal_id
            )
                return false;
            operations.add(row.operation_id);
            journals.add(row.journal_id);
            const net = quantity(row.net_amount, 18),
                tax = quantity(row.tax_amount, 18);
            return (
                net === quantity(requested.net_amount, 18) &&
                tax === quantity(requested.tax_amount, 18) &&
                net + tax === quantity(row.total_amount, 18)
            );
        });
    } catch {
        return false;
    }
}

export function freezeCreditReceipt(value: CreditReceipt): DeepReadonly<CreditReceipt> {
    const copy = JSON.parse(JSON.stringify(value)) as CreditReceipt;
    const freeze = (node: unknown): void => {
        if (node !== null && typeof node === "object") {
            Object.values(node).forEach(freeze);
            Object.freeze(node);
        }
    };
    freeze(copy);
    return copy;
}
