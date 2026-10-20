import type { BusinessPaymentDetail } from "./business-settlement-api";
import type { RefundCapacity } from "./refund-api";
import { parseAmount } from "./money";

export function validRefundCapacity(
    ledgerId: string,
    payment: Pick<
        BusinessPaymentDetail,
        "id" | "ledger_id" | "asset_id" | "party_id" | "direction" | "transaction_date" | "amount"
    >,
    value: unknown,
): value is RefundCapacity {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const row = value as RefundCapacity;
    if (
        payment.ledger_id !== ledgerId ||
        row.ledger_id !== ledgerId ||
        row.id !== payment.id ||
        row.asset_id !== payment.asset_id ||
        row.party_id !== payment.party_id ||
        row.payment_direction !== payment.direction ||
        row.transaction_date !== payment.transaction_date ||
        !Number.isSafeInteger(row.expected_version) ||
        row.expected_version < 1 ||
        !["active", "cancelled"].includes(row.source_status)
    )
        return false;
    try {
        const values = [row.amount, row.applied_amount, row.refunded_amount, row.refundable_amount];
        if (values.some((v) => typeof v !== "string")) return false;
        const [total, applied, refunded, remaining] = values.map((v) => parseAmount(v, 18));
        return (
            total > 0n &&
            total < 10n ** 38n &&
            applied >= 0n &&
            refunded >= 0n &&
            remaining >= 0n &&
            total === parseAmount(payment.amount, 18) &&
            total - applied - refunded === remaining
        );
    } catch {
        return false;
    }
}
