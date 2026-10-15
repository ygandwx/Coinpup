import type { AllocationReturnCapacity } from "./allocation-return-api";
import { returnPaymentAmount } from "./allocation-return-fields";
import { validDraftDate } from "./draft-fields";
import { parseAmount } from "./money";
import type { FrozenSettlement } from "./settlement-intent";

export type AllocationReturnReview = {
    capacity: AllocationReturnCapacity;
    paymentScale: number;
    documentScales: Record<string, number>;
};
const record = (value: unknown): value is Record<string, unknown> =>
    value !== null && typeof value === "object" && !Array.isArray(value);
const uuid = (value: unknown): value is string =>
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u.test(value);
const keys = (value: Record<string, unknown>, names: string[]) =>
    Object.keys(value).every((key) => names.includes(key));
const positive = (value: unknown): value is string => {
    try {
        return typeof value === "string" && parseAmount(value, 18) > 0n;
    } catch {
        return false;
    }
};

export function validReturnInput(value: unknown): boolean {
    if (
        !record(value) ||
        !keys(value, ["id", "payment_id", "return_date", "reason", "lines"]) ||
        !uuid(value.id) ||
        !uuid(value.payment_id) ||
        typeof value.return_date !== "string" ||
        !validDraftDate(value.return_date) ||
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
    return value.lines.every((line) => {
        if (
            !record(line) ||
            !keys(line, [
                "source_allocation_line_id",
                "expected_version",
                "payment_amount",
                "document_amount",
            ]) ||
            !uuid(line.source_allocation_line_id) ||
            !Number.isSafeInteger(line.expected_version) ||
            (line.expected_version as number) < 1 ||
            (line.expected_version as number) > 2147483646 ||
            !positive(line.payment_amount) ||
            !positive(line.document_amount) ||
            sources.has(line.source_allocation_line_id)
        )
            return false;
        sources.add(line.source_allocation_line_id);
        return true;
    });
}

export function validReturnReview(ledgerId: string, body: unknown, reviewed: unknown): boolean {
    if (
        !validReturnInput(body) ||
        !record(body) ||
        !record(reviewed) ||
        !record(reviewed.capacity) ||
        !record(reviewed.documentScales)
    )
        return false;
    const review = reviewed as unknown as AllocationReturnReview,
        capacity = review.capacity;
    if (
        capacity.ledger_id !== ledgerId ||
        capacity.payment_id !== body.payment_id ||
        !uuid(capacity.id) ||
        !validDraftDate(capacity.allocation_date) ||
        (body.return_date as string) < capacity.allocation_date ||
        !Array.isArray(capacity.lines) ||
        capacity.lines.length < 1 ||
        capacity.lines.length > 200 ||
        !capacity.lines.every(record) ||
        new Set(capacity.lines.map((row) => row.source_allocation_line_id)).size !==
            capacity.lines.length
    )
        return false;
    try {
        return (body.lines as Record<string, unknown>[]).every((item) => {
            const source = capacity.lines.find(
                (row) => row.source_allocation_line_id === item.source_allocation_line_id,
            );
            if (
                !source ||
                source.source_status !== "active" ||
                source.expected_version !== 1 ||
                source.expected_version !== item.expected_version ||
                !uuid(source.source_operation_id) ||
                !uuid(source.document_id) ||
                !uuid(source.document_line_id)
            )
                return false;
            const expected = returnPaymentAmount(
                source,
                item.document_amount as string,
                review.paymentScale,
                review.documentScales[source.document_asset_id],
            );
            return (
                parseAmount(expected, review.paymentScale) ===
                parseAmount(item.payment_amount as string, review.paymentScale)
            );
        });
    } catch {
        return false;
    }
}

export function validReturnReceipt(
    value: Record<string, unknown>,
    plan: FrozenSettlement,
): boolean {
    const body = JSON.parse(plan.bodyJson) as Record<string, unknown>;
    const review = JSON.parse(plan.reviewedJson) as AllocationReturnReview;
    if (
        !validReturnReview(plan.ledgerId, body, review) ||
        !keys(value, ["kind", "id", "ledger_id", "payment_id", "return_date", "postings"]) ||
        value.kind !== "business_allocation_return" ||
        value.id !== body.id ||
        value.ledger_id !== plan.ledgerId ||
        value.payment_id !== body.payment_id ||
        value.return_date !== body.return_date ||
        !Array.isArray(value.postings) ||
        value.postings.length !== (body.lines as unknown[]).length
    )
        return false;
    const operations = new Set<string>(),
        journals = new Set<string>();
    const originalOperations = new Set(review.capacity.lines.map((row) => row.source_operation_id));
    return value.postings.every((line, index) => {
        const item = (body.lines as Record<string, unknown>[])[index];
        const source = review.capacity.lines.find(
            (row) => row.source_allocation_line_id === item.source_allocation_line_id,
        )!;
        if (
            !record(line) ||
            !keys(line, [
                "line_no",
                "source_allocation_line_id",
                "source_operation_id",
                "document_id",
                "document_line_id",
                "payment_amount",
                "document_amount",
                "operation_id",
                "journal_id",
            ]) ||
            line.line_no !== index + 1 ||
            line.source_allocation_line_id !== item.source_allocation_line_id ||
            line.source_operation_id !== source.source_operation_id ||
            line.document_id !== source.document_id ||
            line.document_line_id !== source.document_line_id ||
            !uuid(line.operation_id) ||
            !uuid(line.journal_id) ||
            operations.has(line.operation_id) ||
            journals.has(line.journal_id) ||
            originalOperations.has(line.operation_id) ||
            !positive(line.payment_amount) ||
            !positive(line.document_amount) ||
            parseAmount(line.payment_amount, 18) !==
                parseAmount(item.payment_amount as string, 18) ||
            parseAmount(line.document_amount, 18) !==
                parseAmount(item.document_amount as string, 18)
        )
            return false;
        operations.add(line.operation_id);
        journals.add(line.journal_id);
        return true;
    });
}
