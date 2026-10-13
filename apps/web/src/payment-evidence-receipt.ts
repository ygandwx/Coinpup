import type { BusinessPaymentReceipt } from "./business-settlement-api";
import { validSettlementReceipt } from "./settlement-intent";

const record = (value: unknown): value is Record<string, unknown> =>
    typeof value === "object" && value !== null && !Array.isArray(value);
const uuid = (value: unknown): value is string =>
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u.test(value);

/** Derive identity-only validation context; this is never a new financial command. */
export function validOriginalPayment(
    value: unknown,
    ledger: string,
    payment: string,
): value is BusinessPaymentReceipt {
    if (!record(value) || !uuid(payment) || !uuid(ledger) || value.id !== payment) return false;
    if (!("initial_allocation" in value)) return false;
    const group = value.initial_allocation;
    let initial: {
        id: string;
        lines: { document_id: string; document_line_id: string }[];
    } | null = null;
    if (group !== null) {
        if (!record(group) || !uuid(group.id) || !Array.isArray(group.postings)) return false;
        const sources = new Set<string>();
        const lines: { document_id: string; document_line_id: string }[] = [];
        for (const line of group.postings) {
            if (
                !record(line) ||
                !uuid(line.document_id) ||
                !uuid(line.document_line_id) ||
                sources.has(line.document_line_id)
            )
                return false;
            sources.add(line.document_line_id);
            lines.push({ document_id: line.document_id, document_line_id: line.document_line_id });
        }
        initial = { id: group.id, lines };
    }
    return validSettlementReceipt(value, {
        ownerId: "",
        ledgerId: ledger,
        kind: "payment",
        key: "",
        reviewedJson: "",
        bodyJson: JSON.stringify({ id: payment, initial_allocation: initial }),
    });
}

/** Compare every frozen original posting, including the final member of a 200-line receipt. */
export function sameOriginalPayment(
    value: unknown,
    expected: unknown,
    ledger: string,
    payment: string,
): value is BusinessPaymentReceipt {
    if (
        !validOriginalPayment(value, ledger, payment) ||
        !validOriginalPayment(expected, ledger, payment)
    )
        return false;
    if (value.operation_id !== expected.operation_id || value.journal_id !== expected.journal_id)
        return false;
    const left = value.initial_allocation,
        right = expected.initial_allocation;
    if (left === null || right === null) return left === null && right === null;
    return (
        left.id === right.id &&
        left.postings.length === right.postings.length &&
        left.postings.every((row, index) => {
            const original = right.postings[index];
            return (
                row.line_no === original.line_no &&
                row.document_id === original.document_id &&
                row.document_line_id === original.document_line_id &&
                row.operation_id === original.operation_id &&
                row.journal_id === original.journal_id
            );
        })
    );
}
