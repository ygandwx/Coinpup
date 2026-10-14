import type { BusinessPaymentReceipt, PaymentEvidenceTarget } from "./business-settlement-api";
import { validDraftDate } from "./draft-fields";
import { parseAmount } from "./money";
import { validSettlementReceipt } from "./settlement-intent";

const record = (value: unknown): value is Record<string, unknown> =>
    typeof value === "object" && value !== null && !Array.isArray(value);
const uuid = (value: unknown): value is string =>
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u.test(value);
const asset = (value: unknown) =>
    typeof value === "string" && value.length > 0 && value.length <= 200 && !value.includes("\0");
const quantity = (value: unknown, positive: boolean) => {
    try {
        return (
            typeof value === "string" &&
            (positive ? parseAmount(value, 18) > 0n : parseAmount(value, 18) >= 0n)
        );
    } catch {
        return false;
    }
};

/** Reject malformed read metadata before showing it as a reviewed financial fact. */
export function validPaymentEvidenceTarget(
    value: unknown,
    ledger: string,
    payment: string,
): value is PaymentEvidenceTarget {
    if (!record(value) || !record(value.payment)) return false;
    const detail = value.payment;
    return (
        Number.isSafeInteger(value.operation_version) &&
        (value.operation_version as number) > 0 &&
        ["active", "cancelled"].includes(value.operation_status as string) &&
        detail.id === payment &&
        detail.ledger_id === ledger &&
        validOriginalPayment(detail.receipt, ledger, payment) &&
        uuid(detail.account_id) &&
        uuid(detail.party_id) &&
        asset(detail.asset_id) &&
        ["receipt", "payout"].includes(detail.direction as string) &&
        typeof detail.transaction_date === "string" &&
        validDraftDate(detail.transaction_date) &&
        quantity(detail.amount, true) &&
        quantity(detail.applied_amount, false) &&
        quantity(detail.unapplied_amount, false) &&
        typeof detail.description === "string" &&
        typeof detail.created_at === "string" &&
        Array.isArray(detail.fees) &&
        detail.fees.length <= 20 &&
        detail.fee_count === detail.fees.length &&
        detail.fees.every(
            (fee) =>
                record(fee) &&
                uuid(fee.account_id) &&
                uuid(fee.category_id) &&
                asset(fee.asset_id) &&
                quantity(fee.amount, true),
        )
    );
}

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
